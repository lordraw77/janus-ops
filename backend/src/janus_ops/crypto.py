"""Authenticated encryption for small secrets at rest (AES-256-GCM, ADR-004).

Blob format: version (1 byte) || nonce (12 bytes) || ciphertext+tag.
`aad` binds a ciphertext to its owner (e.g. the user id), so a blob copied to another row
does not decrypt.
"""

import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_VERSION = 1


class DecryptionError(Exception):
    pass


class SecretBox:
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
