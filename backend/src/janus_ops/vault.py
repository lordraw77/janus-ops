"""Credential vault with envelope encryption (ADR-004).

Each credential has its own random data key (DEK). The secret is encrypted with the DEK
(AES-256-GCM, separate `nonce` column); the DEK is encrypted ("wrapped") with the key
encryption key derived from JANUS_SECRET_KEY. Both ciphertexts are bound to the credential id,
so rows cannot be swapped. Rotating JANUS_SECRET_KEY later only re-wraps the DEKs
(`kek_version` tells which key wrapped them).

Secrets never leave this module through the API: only `reveal()` returns them, for the
modules that use them (SSH, Proxmox, ...).
"""

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from janus_ops.crypto import Box, DecryptionError

KEK_VERSION = 1

KINDS = (
    "ssh_key",
    "ssh_password",
    "sudo_password",
    "proxmox_token",
    "proxmox_password",
    "fritzbox_password",
    "docker_registry",
    "snmp_v3",
    "notification_secret",
    "generic",
)


class VaultUnavailable(Exception):
    """No master key is configured (JANUS_SECRET_KEY), or the secret manager does not answer."""


class InvalidSecret(ValueError):
    """The submitted secret does not fit the credential kind (message is safe to show)."""


@dataclass(frozen=True)
class Sealed:
    ciphertext: bytes
    nonce: bytes
    dek_wrapped: bytes
    kek_version: int


def _aad(credential_id: str, part: str) -> bytes:
    return f"credential:{credential_id}:{part}".encode()


class SecretStore(Protocol):
    """Where secrets live when their content stays outside the database (KV engine)."""

    def write(self, entity_id: str, payload: dict[str, Any]) -> bytes: ...

    def read(self, entity_id: str, ref: bytes) -> dict[str, Any]: ...

    def delete(self, entity_id: str) -> None: ...


KV_REF = b"kv1:"  # what the ciphertext column holds for a secret kept in the KV engine
KV_KEK_VERSION = 3  # 1 = local key, 2 = Transit, 3 = KV: only a reference in the database


class Vault:
    def __init__(self, box: Box | None, store: SecretStore | None = None) -> None:
        self._box = box
        self._store = store

    @property
    def available(self) -> bool:
        return self._box is not None or self._store is not None

    def discard(self, entity_id: str) -> None:
        """Remove the content of a secret that is kept outside the database (best effort: a
        failure leaves an orphan that `janus-ops secrets prune` finds)."""
        if self._store is not None:
            self._store.delete(entity_id)

    def _kek(self) -> Box:
        if self._box is None:
            raise VaultUnavailable
        return self._box

    def seal(self, credential_id: str, payload: dict[str, Any]) -> Sealed:
        if self._store is not None:
            ref = self._store.write(credential_id, payload)
            return Sealed(ciphertext=ref, nonce=b"", dek_wrapped=b"", kek_version=KV_KEK_VERSION)
        kek = self._kek()
        dek = AESGCM.generate_key(bit_length=256)
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(dek).encrypt(
            nonce, json.dumps(payload).encode(), _aad(credential_id, "secret")
        )
        return Sealed(
            ciphertext=ciphertext,
            nonce=nonce,
            dek_wrapped=kek.encrypt(dek, _aad(credential_id, "dek")),
            kek_version=kek.kek_version,
        )

    def reveal(
        self, credential_id: str, ciphertext: bytes, nonce: bytes, dek_wrapped: bytes
    ) -> dict[str, Any]:
        if bytes(ciphertext).startswith(KV_REF):
            if self._store is None:
                raise VaultUnavailable
            return self._store.read(credential_id, bytes(ciphertext))
        kek = self._kek()
        dek = kek.decrypt(bytes(dek_wrapped), _aad(credential_id, "dek"))
        try:
            plain = AESGCM(dek).decrypt(
                bytes(nonce), bytes(ciphertext), _aad(credential_id, "secret")
            )
        except InvalidTag as exc:
            raise DecryptionError("credential authentication failed") from exc
        data: dict[str, Any] = json.loads(plain)
        return data


def _fingerprint(public_key: Any) -> str:
    blob = public_key.public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).split()[1]
    digest = hashlib.sha256(base64.b64decode(blob)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def _key_type(key: Any) -> str:
    if isinstance(key, ed25519.Ed25519PrivateKey):
        return "ED25519"
    if isinstance(key, rsa.RSAPrivateKey):
        return f"RSA-{key.key_size}"
    if isinstance(key, ec.EllipticCurvePrivateKey):
        return f"ECDSA-{key.curve.name}"
    if isinstance(key, dsa.DSAPrivateKey):
        return "DSA"
    return type(key).__name__


def inspect_ssh_key(pem: str, passphrase: str | None) -> dict[str, str]:
    """Validate an SSH private key (OpenSSH or PEM); return its type and fingerprint."""
    data = pem.strip().encode() + b"\n"
    password = passphrase.encode() if passphrase else None
    loaders = (serialization.load_ssh_private_key, serialization.load_pem_private_key)
    errors: list[str] = []
    for loader in loaders:
        try:
            key = loader(data, password=password)
        except TypeError:
            # cryptography: "password was not given but key is encrypted" (or vice versa)
            raise InvalidSecret(
                "the key is encrypted: enter its passphrase"
                if not password
                else "the key is not encrypted: leave the passphrase empty"
            ) from None
        except UnsupportedAlgorithm as exc:
            raise InvalidSecret(f"unsupported key format: {exc}") from None
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if isinstance(key, dsa.DSAPrivateKey):
            raise InvalidSecret("DSA keys are not supported")
        return {"key_type": _key_type(key), "fingerprint": _fingerprint(key.public_key())}
    # OpenSSH format reports a wrong passphrase as a checksum failure, PEM as a bad decrypt
    markers = ("password", "decrypt", "broken checksum")
    if password and any(m in e.lower() for e in errors for m in markers):
        raise InvalidSecret("wrong passphrase")
    raise InvalidSecret("not a valid SSH private key")
