"""A tiny stand-in for OpenBao / Vault: AppRole login and the Transit encrypt / decrypt endpoints.

Not a security model: it only has to behave like the real API for what Janus-Ops uses, and let a
test break it (revoke tokens, seal, go away)."""

import base64
import json
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROLE_ID = "role-123"
SECRET_ID = "secret-456"


class FakeBao:
    def __init__(self) -> None:
        # one key for every instance: the secret manager outlives a test, like the database does
        self.key = bytes(range(32))
        self.tokens: set[str] = set()
        self.lease = 3600
        self.sealed = False
        self.calls: list[str] = []  # "login", "encrypt", "decrypt", "kv_write", ...
        self.kv: dict[str, list[dict[str, Any]]] = {}  # KV v2: path -> versions (1-based)
        self.server: HTTPServer | None = None

    @property
    def addr(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_port}"

    def revoke_tokens(self) -> None:
        self.tokens.clear()

    def count(self, what: str) -> int:
        return self.calls.count(what)

    def handler(self) -> type[BaseHTTPRequestHandler]:
        bao = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def _send(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict[str, Any]:
                size = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(size) or b"{}")  # type: ignore[no-any-return]

            def _authorized(self) -> bool:
                if bao.sealed:
                    self._send(503, {"errors": ["Vault is sealed"]})
                    return False
                if self.headers.get("X-Vault-Token") not in bao.tokens:
                    self._send(403, {"errors": ["permission denied"]})
                    return False
                return True

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                if url.path.startswith("/v1/sys/health"):
                    return self._send(503 if bao.sealed else 200, {"sealed": bao.sealed})
                if not self._authorized():
                    return None
                query = parse_qs(url.query)
                if url.path.startswith("/v1/secret/data/"):
                    bao.calls.append("kv_read")
                    versions = bao.kv.get(url.path.removeprefix("/v1/secret/data/"))
                    number = int(query.get("version", ["0"])[0]) or len(versions or [])
                    if not versions or not 1 <= number <= len(versions):
                        return self._send(404, {"errors": []})
                    return self._send(
                        200,
                        {"data": {"data": versions[number - 1], "metadata": {"version": number}}},
                    )
                if url.path.startswith("/v1/secret/metadata/") and "list" in query:
                    bao.calls.append("kv_list")
                    prefix = url.path.removeprefix("/v1/secret/metadata/").rstrip("/") + "/"
                    keys = sorted(
                        {p[len(prefix) :].split("/")[0] for p in bao.kv if p.startswith(prefix)}
                    )
                    if not keys:
                        return self._send(404, {"errors": []})
                    return self._send(200, {"data": {"keys": keys}})
                return self._send(404, {})

            def do_DELETE(self) -> None:
                if not self._authorized():
                    return None
                if self.path.startswith("/v1/secret/metadata/"):
                    bao.calls.append("kv_delete")
                    bao.kv.pop(self.path.removeprefix("/v1/secret/metadata/"), None)
                    self.send_response(204)
                    self.end_headers()
                    return None
                return self._send(404, {})

            def do_POST(self) -> None:
                body = self._body()
                if bao.sealed:
                    return self._send(503, {"errors": ["Vault is sealed"]})
                if self.path == "/v1/auth/approle/login":
                    bao.calls.append("login")
                    if body.get("role_id") != ROLE_ID or body.get("secret_id") != SECRET_ID:
                        return self._send(400, {"errors": ["invalid role or secret ID"]})
                    token = "tok-" + secrets.token_hex(8)
                    bao.tokens.add(token)
                    return self._send(
                        200, {"auth": {"client_token": token, "lease_duration": bao.lease}}
                    )
                if self.headers.get("X-Vault-Token") not in bao.tokens:
                    return self._send(403, {"errors": ["permission denied"]})
                if self.path.startswith("/v1/secret/data/"):
                    bao.calls.append("kv_write")
                    versions = bao.kv.setdefault(self.path.removeprefix("/v1/secret/data/"), [])
                    versions.append(body["data"])
                    return self._send(200, {"data": {"version": len(versions)}})
                if self.path == "/v1/transit/encrypt/janus-kek":
                    bao.calls.append("encrypt")
                    nonce = secrets.token_bytes(12)
                    plain = base64.b64decode(body["plaintext"])
                    blob = nonce + AESGCM(bao.key).encrypt(nonce, plain, None)
                    return self._send(
                        200, {"data": {"ciphertext": "vault:v1:" + base64.b64encode(blob).decode()}}
                    )
                if self.path == "/v1/transit/decrypt/janus-kek":
                    bao.calls.append("decrypt")
                    try:
                        blob = base64.b64decode(body["ciphertext"].removeprefix("vault:v1:"))
                        plain = AESGCM(bao.key).decrypt(blob[:12], blob[12:], None)
                    except Exception:  # noqa: BLE001 - any bad ciphertext is a 400 like the real one
                        return self._send(400, {"errors": ["invalid ciphertext"]})
                    return self._send(
                        200, {"data": {"plaintext": base64.b64encode(plain).decode()}}
                    )
                self._send(404, {})

        return Handler


@contextmanager
def fake_bao() -> Iterator[FakeBao]:
    bao = FakeBao()
    bao.server = HTTPServer(("127.0.0.1", 0), bao.handler())
    thread = threading.Thread(target=bao.server.serve_forever, daemon=True)
    thread.start()
    try:
        yield bao
    finally:
        bao.server.shutdown()
        bao.server.server_close()
