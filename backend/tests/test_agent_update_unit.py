"""Signed agent updates (F8, FR-84): signing, verification on the server, and the stream. The agent
is faked; the Go side is tested in agent/internal/tunnel/update_test.go."""

import asyncio
import base64
import hashlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from janus_ops import cli
from janus_ops.agents import update
from janus_ops.agents.mux import CLOSE, DATA, OPEN_FAIL, OPEN_OK, Mux, frame


def _pub(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _signed_dir(tmp_path: Path, key: Ed25519PrivateKey, version: str = "0.3.0") -> Path:
    (tmp_path / "janus-agent-linux-amd64").write_bytes(b"binary-bytes")
    meta = update.sign(key, version, "amd64", b"binary-bytes")
    (tmp_path / "janus-agent-linux-amd64.sig").write_text(json.dumps(meta))
    return tmp_path


def test_a_signed_release_loads(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    release = update.load_release(_signed_dir(tmp_path, key), "amd64", _pub(key))
    assert release.version == "0.3.0"
    assert release.size == len(b"binary-bytes")


def test_release_checks(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    directory = _signed_dir(tmp_path, key)

    def code(arch: str = "amd64", pub: str | None = None) -> str:
        with pytest.raises(update.UpdateError) as exc:
            update.load_release(directory, arch, pub)
        return exc.value.code

    assert code(pub=None) == "release_key_missing"
    assert code(pub=_pub(Ed25519PrivateKey.generate())) == "signature_invalid"  # another key
    assert code("arm64", _pub(key)) == "agent_binary_missing"
    assert code("riscv", _pub(key)) == "unsupported_arch"
    (directory / "janus-agent-linux-amd64").write_bytes(b"tampered")
    assert code(pub=_pub(key)) == "signature_invalid"
    (directory / "janus-agent-linux-amd64.sig").unlink()
    assert code(pub=_pub(key)) == "signature_missing"


def test_cli_keygen_and_sign(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key_file = tmp_path / "release.pem"
    assert cli.main(["agent-keygen", "--private-key", str(key_file)]) == 0
    pub = capsys.readouterr().out.strip().splitlines()[-1]
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert cli.main(["agent-keygen", "--private-key", str(key_file)]) == 2  # never overwritten
    (tmp_path / "janus-agent-linux-arm64").write_bytes(b"arm64 build")
    args = ["agent-sign", "--private-key", str(key_file), "--dir", str(tmp_path)]
    assert cli.main([*args, "--agent-version", "0.3.0"]) == 0
    assert update.load_release(tmp_path, "arm64", pub).version == "0.3.0"
    assert cli.main([*args, "--agent-version", "x"]) == 2


class Wire:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.first = asyncio.Event()
        self.second = asyncio.Event()

    async def send(self, data: bytes) -> None:
        self.sent.append(data)
        self.first.set()
        if len(self.sent) >= 2:
            self.second.set()


def _hub(mux: Mux, features: frozenset[str] = frozenset()) -> object:
    class Hub:
        def get(self, node_id: str) -> object:
            return SimpleNamespace(mux=mux, features=features)

    return Hub()


async def _push(reason: bytes | None, refuse: bytes | None, tmp_path: Path) -> tuple[str, Wire]:
    key = Ed25519PrivateKey.generate()
    release = update.load_release(_signed_dir(tmp_path, key), "amd64", _pub(key))
    wire = Wire()
    mux = Mux(wire.send)

    async def agent() -> None:
        await wire.first.wait()
        _, sid = struct.unpack(">BI", wire.sent[0][:5])
        request = json.loads(wire.sent[0][5:])
        assert request["kind"] == "update"
        assert request["sha256"] == hashlib.sha256(b"binary-bytes").hexdigest()
        assert request["size"] == len(b"binary-bytes")
        if refuse is not None:
            mux.dispatch(frame(OPEN_FAIL, sid, refuse))
            return
        mux.dispatch(frame(OPEN_OK, sid))
        await wire.second.wait()
        assert wire.sent[1][0] == DATA
        assert wire.sent[1][5:] == b"binary-bytes"
        mux.dispatch(frame(CLOSE, sid, reason or b""))

    task = asyncio.create_task(agent())
    try:
        result = await update.push(_hub(mux), "n1", release)  # type: ignore[arg-type]
    finally:
        await task
    return result, wire


def test_push_returns_the_installed_version(tmp_path: Path) -> None:
    result, _ = asyncio.run(_push(b"updated:0.3.0", None, tmp_path))
    assert result == "0.3.0"


@pytest.mark.parametrize(
    ("reason", "refuse", "code"),
    [
        (b"failed:checksum", None, "update_checksum"),
        (b"", None, "update_failed"),
        (None, b"not_newer", "not_newer"),
        (None, b"bad_signature", "bad_signature"),
        (None, b"update_not_configured", "update_not_configured"),
        (None, b"unknown_kind", "agent_outdated"),
    ],
)
def test_push_failures_have_stable_codes(
    tmp_path: Path, reason: bytes | None, refuse: bytes | None, code: str
) -> None:
    with pytest.raises(update.UpdateError) as exc:
        asyncio.run(_push(reason, refuse, tmp_path))
    assert exc.value.code == code


def test_update_all_is_declared_before_the_node_routes() -> None:
    from janus_ops.agents.router import router

    paths = [r.path for r in router.routes]  # type: ignore[attr-defined]
    assert "/agents/update-all" in paths


def test_cli_verify_accepts_good_signatures_and_refuses_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "release.pem"
    assert cli.main(["agent-keygen", "--private-key", str(key_file)]) == 0
    pub = capsys.readouterr().out.strip().splitlines()[-1]
    (tmp_path / "janus-agent-linux-amd64").write_bytes(b"amd64 build")
    (tmp_path / "janus-agent-linux-arm").write_bytes(b"arm build")
    sign = ["agent-sign", "--private-key", str(key_file), "--dir", str(tmp_path)]
    assert cli.main([*sign, "--agent-version", "1.2.3"]) == 0
    capsys.readouterr()
    verify = ["agent-verify", "--dir", str(tmp_path), "--public-key", pub]
    assert cli.main(verify) == 0
    assert cli.main([*verify, "--agent-version", "1.2.3"]) == 0
    assert cli.main([*verify, "--agent-version", "9.9.9"]) == 1  # signed as another version
    other = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    assert (
        cli.main(
            [
                "agent-verify",
                "--dir",
                str(tmp_path),
                "--public-key",
                base64.b64encode(other).decode(),
            ]
        )
        == 1
    )
    (tmp_path / "janus-agent-linux-arm").write_bytes(b"tampered")
    assert cli.main(verify) == 1  # the binary is not the signed one
    assert cli.main(["agent-verify", "--dir", str(tmp_path / "empty"), "--public-key", pub]) == 1


# ------------------------------------------------------------------ flow control (update_ack)
async def _windowed_run(
    tmp_path: Path, size: int, ack: bool, stall_s: float | None = None
) -> tuple[str | Exception, int, int]:
    """A fake agent that confirms every 64 KiB (or never). Returns the outcome, the largest amount
    the server ever had unconfirmed, and how many bytes it sent."""
    key = Ed25519PrivateKey.generate()
    (tmp_path / "janus-agent-linux-amd64").write_bytes(b"x" * size)
    meta = update.sign(key, "0.3.0", "amd64", b"x" * size)
    (tmp_path / "janus-agent-linux-amd64.sig").write_text(json.dumps(meta))
    release = update.load_release(tmp_path, "amd64", _pub(key))
    wire = Wire()
    mux = Mux(wire.send)
    stats = {"max_unconfirmed": 0, "received": 0, "acked": 0}

    async def agent() -> None:
        await wire.first.wait()
        _, sid = struct.unpack(">BI", wire.sent[0][:5])
        mux.dispatch(frame(OPEN_OK, sid))
        seen = 1
        since = 0
        while stats["received"] < size:
            await asyncio.sleep(0)
            while seen < len(wire.sent):
                payload = wire.sent[seen][5:]
                seen += 1
                stats["received"] += len(payload)
                since += len(payload)
                stats["max_unconfirmed"] = max(
                    stats["max_unconfirmed"], stats["received"] - stats["acked"]
                )
                if ack and since >= 64 * 1024:
                    since = 0
                    stats["acked"] = stats["received"]
                    mux.dispatch(frame(DATA, sid, stats["acked"].to_bytes(8, "big")))
            if not ack and stats["received"] >= update.WINDOW + 64 * 1024:
                return  # an agent that stopped confirming: the server must stall, not push on
        mux.dispatch(frame(CLOSE, sid, b"updated:0.3.0"))

    task = asyncio.create_task(agent())
    old_stall = update.STALL_S
    if stall_s is not None:
        update.STALL_S = stall_s
    try:
        outcome: str | Exception = await update.push(
            _hub(mux, frozenset({"streams", "update_ack"})),  # type: ignore[arg-type]
            "n1",
            release,
        )
    except update.UpdateError as exc:
        outcome = exc
    finally:
        update.STALL_S = old_stall
        task.cancel()
    return outcome, stats["max_unconfirmed"], stats["received"]


def test_windowed_update_never_runs_far_ahead_of_the_agent(tmp_path: Path) -> None:
    size = 2 * 1024 * 1024
    outcome, max_unconfirmed, received = asyncio.run(_windowed_run(tmp_path, size, ack=True))
    assert outcome == "0.3.0" and received == size
    # the window plus the one block in flight: far below the 2 MiB of the whole binary
    assert max_unconfirmed <= update.WINDOW + 64 * 1024


def test_an_agent_that_stops_confirming_stalls_the_update_not_the_link(tmp_path: Path) -> None:
    outcome, _, received = asyncio.run(
        _windowed_run(tmp_path, 2 * 1024 * 1024, ack=False, stall_s=0.2)
    )
    assert isinstance(outcome, update.UpdateError) and outcome.code == "update_stalled"
    assert received <= update.WINDOW + 128 * 1024  # it did not keep pushing megabytes


def test_an_agent_without_the_feature_still_gets_the_binary_in_one_go(tmp_path: Path) -> None:
    result, _ = asyncio.run(_push(b"updated:0.3.0", None, tmp_path))
    assert result == "0.3.0"  # the existing path, unchanged
