"""Local password hashing (break-glass account only) with stdlib scrypt.

Format: scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>
"""

import base64
import hashlib
import hmac
import secrets

_N, _R, _P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024  # scrypt needs 128 * r * n bytes = 32 MiB
_DKLEN = 32
MIN_LENGTH = 16


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"password must be at least {MIN_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=_DKLEN
    )
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(hash_b64, validate=True)
        digest = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        # Malformed hash in the database: treat as a failed verification, never crash login
        return False
    return hmac.compare_digest(digest, expected)
