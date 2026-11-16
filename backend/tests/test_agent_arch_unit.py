"""Agent binaries per CPU (FR-20): which file a node gets for its `uname -m`. No network."""

import pytest

from janus_ops.agents.bootstrap import ARCHES, BINARY


@pytest.mark.parametrize(
    ("machine", "binary"),
    [
        ("x86_64", "janus-agent-linux-amd64"),
        ("aarch64", "janus-agent-linux-arm64"),
        # a 32-bit Raspberry Pi (2, 3, and later on a 32-bit OS) reports armv7l
        ("armv7l", "janus-agent-linux-arm"),
    ],
)
def test_each_supported_machine_gets_its_binary(machine: str, binary: str) -> None:
    assert BINARY.format(arch=ARCHES[machine]) == binary


@pytest.mark.parametrize("machine", ["armv6l", "i686", "riscv64", "ppc64le"])
def test_other_machines_are_not_supported(machine: str) -> None:
    assert machine not in ARCHES
