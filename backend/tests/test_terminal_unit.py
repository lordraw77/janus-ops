"""Pure helpers of the bastion: snippet keystrokes, SFTP path checks, download file names."""

import pytest
from fastapi import HTTPException

from janus_ops.terminal.sftp import _check_path, _content_disposition
from janus_ops.terminal.snippets import keystrokes


@pytest.mark.parametrize(
    ("body", "typed"),
    [
        ("uptime", "uptime\r"),
        ("uptime\n", "uptime\r"),
        ("cd /tmp\r\nls -la\n\n", "cd /tmp\rls -la\r"),
        ("a\n\nb", "a\r\rb\r"),  # blank lines in the middle are kept
    ],
)
def test_keystrokes(body: str, typed: str) -> None:
    assert keystrokes(body) == typed


@pytest.mark.parametrize(
    ("path", "normalized"),
    [("/", "/"), ("/etc/", "/etc"), ("/a/./b/../c", "/a/c"), ("//srv", "//srv")],
)
def test_check_path(path: str, normalized: str) -> None:
    assert _check_path(path) == normalized


@pytest.mark.parametrize("path", ["", "etc", "./x", "/a\x00b", "/" + "a" * 5000])
def test_check_path_rejects(path: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _check_path(path)
    assert exc.value.detail == "invalid_path"


def test_content_disposition_is_header_safe() -> None:
    value = _content_disposition('ré"port\\.txt')
    assert value.startswith('attachment; filename="r?_port_.txt"')
    assert value.endswith("filename*=UTF-8''r%C3%A9%22port%5C.txt")
