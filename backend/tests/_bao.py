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

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROLE_ID = "role-123"
SECRET_ID = "secret-456"


class FakeBao:
    def __init__(self) -> None:
        self.key = AESGCM.generate_key(bit_length=256)
        self.tokens: set[str] = set()
        self.lease = 3600
        self.sealed = False
        self.calls: list[str] = []  # "login", "encrypt", "decrypt"
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

            def do_GET(self) -> None:
                if self.path.startswith("/v1/sys/health"):
                    self._send(503 if bao.sealed else 200, {"sealed": bao.sealed})
                else:
                    self._send(404, {})

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
