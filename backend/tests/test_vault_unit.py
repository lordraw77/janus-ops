"""Vault and inventory validation without a database."""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from fastapi.testclient import TestClient

from janus_ops.crypto import DecryptionError, SecretBox
from janus_ops.inventory.nodes import _valid_address
from janus_ops.vault import InvalidSecret, Vault, VaultUnavailable, inspect_ssh_key

KEY = "33" * 32


def openssh_key(passphrase: bytes | None = None) -> str:
    key = ed25519.Ed25519PrivateKey.generate()
    enc = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, enc
    ).decode()


def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


# ---------------------------------------------------------------- envelope encryption
def test_seal_and_reveal_round_trip() -> None:
    vault = Vault(SecretBox(KEY))
    sealed = vault.seal("cred-1", {"secret": "s3cr3t-value"})
    assert b"s3cr3t-value" not in sealed.ciphertext
    assert len(sealed.nonce) == 12 and sealed.kek_version == 1
    assert vault.reveal("cred-1", sealed.ciphertext, sealed.nonce, sealed.dek_wrapped) == {
        "secret": "s3cr3t-value"
    }


def test_each_credential_gets_its_own_data_key() -> None:
    vault = Vault(SecretBox(KEY))
    a = vault.seal("cred-1", {"secret": "same"})
    b = vault.seal("cred-1", {"secret": "same"})
    assert a.dek_wrapped != b.dek_wrapped and a.ciphertext != b.ciphertext


def test_ciphertext_bound_to_its_credential() -> None:
    vault = Vault(SecretBox(KEY))
    sealed = vault.seal("cred-1", {"secret": "x"})
    with pytest.raises(DecryptionError):  # row copied onto another credential
        vault.reveal("cred-2", sealed.ciphertext, sealed.nonce, sealed.dek_wrapped)
    with pytest.raises(DecryptionError):  # tampered ciphertext
        vault.reveal("cred-1", sealed.ciphertext[:-1] + b"\x00", sealed.nonce, sealed.dek_wrapped)
    with pytest.raises(DecryptionError):  # other master key
        Vault(SecretBox("44" * 32)).reveal(
            "cred-1", sealed.ciphertext, sealed.nonce, sealed.dek_wrapped
        )


def test_vault_without_key() -> None:
    vault = Vault(None)
    assert not vault.available
    with pytest.raises(VaultUnavailable):
        vault.seal("cred-1", {"secret": "x"})


# ---------------------------------------------------------------- SSH keys
def test_inspect_openssh_ed25519() -> None:
    info = inspect_ssh_key(openssh_key(), None)
    assert info["key_type"] == "ED25519"
    assert info["fingerprint"].startswith("SHA256:") and len(info["fingerprint"]) == 50


def test_inspect_pem_rsa() -> None:
    assert inspect_ssh_key(rsa_pem(), None)["key_type"] == "RSA-2048"


def test_inspect_encrypted_key() -> None:
    pem = openssh_key(b"correct horse")
    assert inspect_ssh_key(pem, "correct horse")["key_type"] == "ED25519"
    with pytest.raises(InvalidSecret, match="passphrase"):
        inspect_ssh_key(pem, None)
    with pytest.raises(InvalidSecret, match="wrong passphrase"):
        inspect_ssh_key(pem, "wrong")


def test_inspect_plain_key_with_passphrase() -> None:
    with pytest.raises(InvalidSecret, match="not encrypted"):
        inspect_ssh_key(openssh_key(), "unneeded")


@pytest.mark.parametrize(
    "garbage",
    ["", "hello", "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"],
)
def test_inspect_rejects_garbage(garbage: str) -> None:
    with pytest.raises(InvalidSecret, match="not a valid SSH private key"):
        inspect_ssh_key(garbage, None)


def test_inspect_public_key_is_rejected() -> None:
    pub = (
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    with pytest.raises(InvalidSecret):
        inspect_ssh_key(pub, None)


# ---------------------------------------------------------------- addresses
@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("10.0.0.1", "10.0.0.1"),
        (" 192.168.1.20 ", "192.168.1.20"),
        ("2001:DB8::1", "2001:db8::1"),
        ("PVE01.Lab.Example.", "pve01.lab.example"),
        ("docker-2", "docker-2"),
    ],
)
def test_valid_addresses(raw: str, normalized: str) -> None:
    assert _valid_address(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    ["", "-bad.example", "bad-.example", "a..b", "host name", "x" * 64 + ".com", "1.2.3.4/24"],
)
def test_invalid_addresses(raw: str) -> None:
    with pytest.raises(ValueError):
        _valid_address(raw)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v1/nodes"),
        ("post", "/api/v1/nodes"),
        ("get", "/api/v1/nodes/00000000-0000-0000-0000-000000000001"),
        ("patch", "/api/v1/nodes/00000000-0000-0000-0000-000000000001"),
        ("delete", "/api/v1/nodes/00000000-0000-0000-0000-000000000001"),
        ("get", "/api/v1/groups"),
        ("post", "/api/v1/groups"),
        ("get", "/api/v1/tags"),
        ("get", "/api/v1/credentials"),
        ("post", "/api/v1/credentials"),
        ("put", "/api/v1/credentials/00000000-0000-0000-0000-000000000001/secret"),
        ("delete", "/api/v1/credentials/00000000-0000-0000-0000-000000000001"),
    ],
)
def test_inventory_endpoints_require_session(client: TestClient, method: str, path: str) -> None:
    kwargs = {"json": {}} if method in ("post", "patch", "put") else {}
    assert getattr(client, method)(path, **kwargs).status_code in (401, 422)
