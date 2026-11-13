"""TOTP (RFC 6238: SHA-1, 6 digits, 30 s) computed locally — no network access needed.

Works behind corporate proxies and air-gapped: the QR code is rendered on the server and the
code depends only on the shared secret and the clock (keep the server in sync with NTP).
"""

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from urllib.parse import quote, urlencode

import segno

STEP_S = 30
DIGITS = 6
WINDOW = 1  # accept the previous and next step: tolerates ~30 s of clock drift
ISSUER = "Janus-Ops"
RECOVERY_CODES = 10


def new_secret() -> str:
    """160-bit secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret_b32: str) -> bytes:
    padded = secret_b32.upper() + "=" * (-len(secret_b32) % 8)
    return base64.b32decode(padded)


def code_at(secret_b32: str, step: int) -> str:
    digest = hmac.new(_key(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_S)


def verify(
    secret_b32: str, code: str, *, last_step: int | None, now: float | None = None
) -> int | None:
    """Return the matched step, or None. Steps <= last_step are rejected (no replay)."""
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    base = current_step(now)
    for step in range(base - WINDOW, base + WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(code_at(secret_b32, step), code):
            return step
    return None


def provisioning_uri(secret_b32: str, account: str) -> str:
    label = quote(f"{ISSUER}:{account}")
    query = urlencode({"secret": secret_b32, "issuer": ISSUER, "digits": DIGITS, "period": STEP_S})
    return f"otpauth://totp/{label}?{query}"


def qr_svg(uri: str) -> str:
    buf = io.BytesIO()
    segno.make(uri, error="m").save(
        buf, kind="svg", scale=5, border=2, dark="#090A0F", light="#F1F5F9"
    )
    return buf.getvalue().decode()


def new_recovery_codes() -> list[str]:
    """10 codes like 'k3x9-2mpq-7fhd-wq4n' (80 bits each)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return [
        "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))
        for _ in range(RECOVERY_CODES)
    ]


def hash_recovery_code(code: str) -> bytes:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).digest()
