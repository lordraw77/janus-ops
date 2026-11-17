"""Master key in an external secret manager: OpenBao or HashiCorp Vault, Transit engine (FR-86,
ADR-011 option a).

The key never leaves the secret manager. Janus-Ops asks Transit to wrap and unwrap the small data
keys (DEKs) of its secrets; the secrets themselves stay encrypted in Postgres. Access is by AppRole
(`role_id` + `secret_id`); the token is renewed by logging in again before it expires. The policy
the role needs is only `update` on `<mount>/encrypt/<key>` and `<mount>/decrypt/<key>`.

The client is synchronous (the vault is), uses the standard library only, always verifies TLS
(`http://` is accepted for loopback only, for local tests) and never logs a secret, a token or a
response body.
"""

import base64
import hashlib
import json
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from janus_ops.config import Settings
from janus_ops.crypto import Box, CompositeBox, DecryptionError, SecretBox
from janus_ops.vault import VaultUnavailable

TRANSIT_PREFIX = b"T"  # first byte of a blob sealed with Transit (local blobs start with 0x01)
_FORMAT = 1
CACHE_MAX = 1024
RENEW_AT = 0.8  # log in again after 80% of the token's lifetime


class SecretManagerUnavailable(VaultUnavailable):
    """The secret manager cannot be reached, is sealed, or refuses this role."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TransitConfig:
    addr: str
    role_id: str
    secret_id: str
    mount: str = "transit"
    key: str = "janus-kek"
    ca_file: Path | None = None
    timeout_s: int = 5
    dek_cache_s: int = 300


def _is_loopback(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1")


class TransitClient:
    def __init__(self, config: TransitConfig) -> None:
        parts = urlsplit(config.addr)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("the secret manager address must be an https:// URL")
        if parts.scheme == "http" and not _is_loopback(parts.hostname):
            raise ValueError("the secret manager address must be https:// (http only on loopback)")
        self._config = config
        self._base = config.addr.rstrip("/")
        self._ctx: ssl.SSLContext | None = None
        if parts.scheme == "https":
            self._ctx = ssl.create_default_context(
                cafile=str(config.ca_file) if config.ca_file else None
            )
        self._lock = threading.Lock()
        self._token: str | None = None
        self._renew_at = 0.0

    # ------------------------------------------------------------------ HTTP
    def _request(
        self, method: str, path: str, body: dict[str, Any] | None, token: str | None
    ) -> tuple[int, dict[str, Any]]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method)  # noqa: S310
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("X-Vault-Token", token)
        try:
            with urllib.request.urlopen(  # noqa: S310
                req, timeout=self._config.timeout_s, context=self._ctx
            ) as resp:
                return resp.status, _json(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read())
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError):
            raise SecretManagerUnavailable("unreachable") from None

    def _login(self) -> str:
        status, out = self._request(
            "POST",
            "/v1/auth/approle/login",
            {"role_id": self._config.role_id, "secret_id": self._config.secret_id},
            None,
        )
        auth = out.get("auth") or {}
        token = auth.get("client_token")
        if status != 200 or not isinstance(token, str):
            raise SecretManagerUnavailable("sealed" if status == 503 else "login_refused")
        lease = int(auth.get("lease_duration") or 0)
        self._token = token
        # a token without a lease (a periodic or root token) is renewed every few minutes anyway
        self._renew_at = time.monotonic() + (lease * RENEW_AT if lease else 300)
        return token

    def _token_now(self, force: bool = False) -> str:
        with self._lock:
            if force or self._token is None or time.monotonic() >= self._renew_at:
                return self._login()
            return self._token

    def _call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        status, out = self._request("POST", path, body, self._token_now())
        if status == 403:  # revoked or expired early: one fresh login, then give up
            status, out = self._request("POST", path, body, self._token_now(force=True))
        if status == 200 and isinstance(out.get("data"), dict):
            data: dict[str, Any] = out["data"]
            return data
        if status in (400, 404):
            raise DecryptionError("the secret manager refused the data")
        raise SecretManagerUnavailable({403: "denied", 503: "sealed"}.get(status, f"http_{status}"))

    # ------------------------------------------------------------------ Transit
    def wrap(self, plaintext: bytes) -> str:
        data = self._call(
            f"/v1/{self._config.mount}/encrypt/{self._config.key}",
            {"plaintext": base64.b64encode(plaintext).decode()},
        )
        ciphertext = data.get("ciphertext")
        if not isinstance(ciphertext, str):
            raise SecretManagerUnavailable("bad_response")
        return ciphertext

    def unwrap(self, ciphertext: str) -> bytes:
        data = self._call(
            f"/v1/{self._config.mount}/decrypt/{self._config.key}", {"ciphertext": ciphertext}
        )
        try:
            return base64.b64decode(str(data["plaintext"]), validate=True)
        except (KeyError, ValueError):
            raise SecretManagerUnavailable("bad_response") from None

    def health(self) -> bool:
        """Reachable, initialised and unsealed. Needs no token."""
        try:
            status, _ = self._request("GET", "/v1/sys/health?standbyok=true", None, None)
        except SecretManagerUnavailable:
            return False
        return status in (200, 429)


def _json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


class TransitBox:
    """A `Box` (see crypto.py) whose key lives in Transit.

    Each blob has its own random data key. The data key is wrapped by Transit together with a hash
    of `aad`, so a blob copied to another row does not unwrap to the same data key: the hash is
    checked, whatever the key type of the Transit key. Blob: `T` || format || length of the
    wrapped key (2 bytes) || wrapped key || nonce || ciphertext+tag.
    """

    kek_version = 2  # stored next to a wrapped data key: 1 = local key, 2 = Transit

    def __init__(self, client: TransitClient, cache_s: int = 300) -> None:
        self._client = client
        self._cache_s = cache_s
        self._cache: dict[bytes, tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def check(self) -> bool:
        return self._client.health()

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        dek = AESGCM.generate_key(bit_length=256)
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        wrapped = self._client.wrap(hashlib.sha256(aad).digest() + dek).encode()
        return (
            TRANSIT_PREFIX
            + bytes([_FORMAT])
            + len(wrapped).to_bytes(2, "big")
            + wrapped
            + (nonce + ciphertext)
        )

    def _dek(self, wrapped: bytes, aad: bytes) -> bytes:
        cache_key = hashlib.sha256(wrapped + aad).digest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit and hit[0] > now:
                return hit[1]
        plain = self._client.unwrap(wrapped.decode())
        if len(plain) != 64 or not secrets.compare_digest(plain[:32], hashlib.sha256(aad).digest()):
            raise DecryptionError("the data key belongs to another record")
        dek = plain[32:]
        if self._cache_s > 0:
            with self._lock:
                if len(self._cache) >= CACHE_MAX:
                    self._cache.clear()
                self._cache[cache_key] = (now + self._cache_s, dek)
        return dek

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        if len(blob) < 4 or blob[:1] != TRANSIT_PREFIX or blob[1] != _FORMAT:
            raise DecryptionError("unsupported format")
        size = int.from_bytes(blob[2:4], "big")
        wrapped, rest = blob[4 : 4 + size], blob[4 + size :]
        if len(wrapped) != size or len(rest) < 12 + 16:
            raise DecryptionError("truncated")
        dek = self._dek(wrapped, aad)
        try:
            return AESGCM(dek).decrypt(rest[:12], rest[12:], aad)
        except InvalidTag as exc:
            raise DecryptionError("authentication failed") from exc


def _transit_box(settings: Settings) -> TransitBox | None:
    if not (settings.bao_addr and settings.bao_role_id and settings.bao_secret_id):
        return None
    client = TransitClient(
        TransitConfig(
            addr=settings.bao_addr,
            role_id=settings.bao_role_id,
            secret_id=settings.bao_secret_id.get_secret_value(),
            mount=settings.bao_transit_mount,
            key=settings.bao_transit_key,
            ca_file=settings.bao_ca_file,
            timeout_s=settings.bao_timeout_s,
            dek_cache_s=settings.bao_dek_cache_s,
        )
    )
    return TransitBox(client, settings.bao_dek_cache_s)


def build_box(settings: Settings, primary: str | None = None) -> Box | None:
    """The box for the configured backend (or for `primary`, when moving secrets between
    backends). It reads what either backend wrote, as far as that backend is configured: with
    Transit the local key, while still set, reads what it wrote before the move."""
    backend = primary or settings.secret_backend
    local = SecretBox(settings.secret_key.get_secret_value()) if settings.secret_key else None
    transit = _transit_box(settings)
    if backend == "local":
        if transit is None:
            return local
        if local is None:
            raise ValueError("JANUS_SECRET_KEY is not set")
        return CompositeBox(local, {bytes([1]): local, TRANSIT_PREFIX: transit})
    if transit is None:
        raise ValueError("the secret manager is not configured")
    readers: dict[bytes, Box] = {TRANSIT_PREFIX: transit}
    if local is not None:
        readers[bytes([1])] = local
    return CompositeBox(transit, readers)
