"""Authenticated encryption for small secrets at rest (AES-256-GCM, ADR-004).

Blob format: version (1 byte) || nonce (12 bytes) || ciphertext+tag.
`aad` binds a ciphertext to its owner (e.g. the user id), so a blob copied to another row
does not decrypt.
"""

import secrets
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = 1


class DecryptionError(Exception):
    pass


class Box(Protocol):
    """Encrypts small secrets bound to an owner (`aad`). SecretBox keeps the key in the process
    (JANUS_SECRET_KEY); TransitBox keeps it in an external secret manager (FR-86)."""

    @property
    def kek_version(self) -> int: ...

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes: ...

    def decrypt(self, blob: bytes, aad: bytes) -> bytes: ...


class SecretBox:
    kek_version = 1  # stored next to a wrapped data key: 1 = local key, 2 = Transit

    def __init__(self, key_hex: str) -> None:
        key = bytes.fromhex(key_hex)
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(12)
        return bytes([_VERSION]) + nonce + self._aead.encrypt(nonce, plaintext, aad)

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        if len(blob) < 1 + 12 + 16 or blob[0] != _VERSION:
            raise DecryptionError("unsupported format")
        try:
            return self._aead.decrypt(blob[1:13], blob[13:], aad)
        except InvalidTag as exc:
            raise DecryptionError("authentication failed") from exc


class CompositeBox:
    """New secrets go to `primary`; old ones are read by whichever box wrote them (the first byte
    of a blob says which). This is what makes a move between backends gradual and reversible."""

    def __init__(self, primary: Box, readers: dict[bytes, Box]) -> None:
        self._primary = primary
        self._readers = readers

    @property
    def kek_version(self) -> int:
        return self._primary.kek_version

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        return self._primary.encrypt(plaintext, aad)

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        reader = self._readers.get(blob[:1])
        if reader is None:
            raise DecryptionError("unsupported format, or its key is not configured")
        return reader.decrypt(blob, aad)

    def check(self) -> bool | None:
        """Readiness of the secret manager, or None when no secret manager is used."""
        for reader in self._readers.values():
            check = getattr(reader, "check", None)
            if check is not None:
                return bool(check())
        return None
