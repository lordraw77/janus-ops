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
from typing import Any

from cryptography.exceptions import InvalidTag, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from janus_ops.crypto import DecryptionError, SecretBox

KEK_VERSION = 1

KINDS = (
    "ssh_key",
    "ssh_password",
    "sudo_password",
    "proxmox_token",
    "docker_registry",
    "snmp_v3",
    "notification_secret",
    "generic",
)


class VaultUnavailable(Exception):
    """JANUS_SECRET_KEY is not configured."""


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


class Vault:
    def __init__(self, box: SecretBox | None) -> None:
        self._box = box

    @property
    def available(self) -> bool:
        return self._box is not None

    def _kek(self) -> SecretBox:
        if self._box is None:
            raise VaultUnavailable
        return self._box

    def seal(self, credential_id: str, payload: dict[str, Any]) -> Sealed:
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
            kek_version=KEK_VERSION,
        )

    def reveal(
        self, credential_id: str, ciphertext: bytes, nonce: bytes, dek_wrapped: bytes
    ) -> dict[str, Any]:
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
