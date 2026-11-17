"""Signed self-update of the agent (F8, FR-84, C8).

The release key is generated and kept OFF the server (`janus-ops agent-keygen`, `agent-sign`). The
server holds only the public key and the detached signatures next to the binaries, so it can
distribute an update but cannot forge one. The agent verifies the signature against the public key
in its own configuration before replacing itself (agent/internal/tunnel/update.go); the server
checks it too, to fail early with a clear reason.

Signature file `janus-agent-linux-<arch>.sig`: JSON {version, arch, sha256, signature}, where
`signature` is the base64 ed25519 signature over `message(version, arch, sha256)`.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from janus_ops.agents.hub import AgentHub
from janus_ops.agents.mux import StreamError

CONTEXT = "janus-agent-update-v1"
BINARY = "janus-agent-linux-{arch}"
ARCHES = ("amd64", "arm64", "arm")
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}$")
MAX_SIZE = 64 * 1024 * 1024
RESULT_WAIT_S = 60


class UpdateError(Exception):
    """Refused before or while updating; `code` is stable and shown to the user."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def message(version: str, arch: str, sha256: str) -> bytes:
    return f"{CONTEXT}\n{version}\n{arch}\n{sha256}".encode()


def sign(private_key: Ed25519PrivateKey, version: str, arch: str, binary: bytes) -> dict[str, str]:
    sha = hashlib.sha256(binary).hexdigest()
    return {
        "version": version,
        "arch": arch,
        "sha256": sha,
        "signature": base64.b64encode(private_key.sign(message(version, arch, sha))).decode(),
    }


def public_key(b64: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(b64, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, binascii.Error):
        raise UpdateError("release_key_invalid") from None


@dataclass(frozen=True)
class Release:
    version: str
    arch: str
    sha256: str
    signature: str
    path: Path
    size: int


def load_release(agent_dir: Path, arch: str, release_key_b64: str | None) -> Release:
    """The signed binary for `arch`, verified against the release public key."""
    if release_key_b64 is None:
        raise UpdateError("release_key_missing")
    if arch not in ARCHES:
        raise UpdateError("unsupported_arch")
    binary = agent_dir / BINARY.format(arch=arch)
    sig_file = agent_dir / (BINARY.format(arch=arch) + ".sig")
    if not binary.is_file():
        raise UpdateError("agent_binary_missing")
    if not sig_file.is_file():
        raise UpdateError("signature_missing")
    try:
        meta = json.loads(sig_file.read_text())
        version, sha, signature = str(meta["version"]), str(meta["sha256"]), str(meta["signature"])
    except (ValueError, KeyError, OSError):
        raise UpdateError("signature_invalid") from None
    size = binary.stat().st_size
    if not VERSION_RE.match(version) or meta.get("arch") != arch or not 0 < size <= MAX_SIZE:
        raise UpdateError("signature_invalid")
    try:
        public_key(release_key_b64).verify(base64.b64decode(signature), message(version, arch, sha))
    except (InvalidSignature, ValueError, binascii.Error):
        raise UpdateError("signature_invalid") from None
    if hashlib.sha256(binary.read_bytes()).hexdigest() != sha:
        raise UpdateError("signature_invalid")  # the binary is not the signed one
    return Release(version, arch, sha, signature, binary, size)


async def push(hub: AgentHub, node_id: str, release: Release) -> str:
    """Send the binary; returns the version the agent reports having installed."""
    link = hub.get(node_id)
    if link is None:
        raise UpdateError("agent_offline")
    if link.mux is None:
        raise UpdateError("agent_outdated")
    try:
        stream = await link.mux.open(
            "update",
            version=release.version,
            sha256=release.sha256,
            signature=release.signature,
            size=release.size,
        )
    except StreamError as exc:
        # an agent that predates updates answers unknown_kind
        raise UpdateError("agent_outdated" if exc.code == "unknown_kind" else exc.code) from None
    data = await asyncio.to_thread(release.path.read_bytes)
    try:
        await stream.write(data)
        await asyncio.wait_for(_drain(stream), RESULT_WAIT_S)
    except StreamError:
        raise UpdateError("update_failed") from None
    except TimeoutError:
        await stream.close()
        raise UpdateError("update_timeout") from None
    reason = stream.close_reason or ""
    if reason.startswith("updated:"):
        return reason[len("updated:") :]
    if reason.startswith("failed:"):
        raise UpdateError("update_" + reason[len("failed:") :])
    raise UpdateError("update_failed")


async def _drain(stream: object) -> None:
    while await stream.read():  # type: ignore[attr-defined]
        pass
