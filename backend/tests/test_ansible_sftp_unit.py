"""SFTP sources (F6): URL parsing and the canonical form kept in the source config."""

import pytest

from janus_ops.ansible.catalog import CatalogError
from janus_ops.ansible.sftp_source import canonical_url, parse_url


def test_url_with_user_port_and_directory() -> None:
    parsed = parse_url("sftp://deploy@files.example.lan:2222/srv/ansible/")
    assert parsed == {
        "host": "files.example.lan",
        "port": 2222,
        "username": "deploy",
        "path": "/srv/ansible",
    }
    assert canonical_url(parsed) == "sftp://deploy@files.example.lan:2222/srv/ansible"


def test_url_without_user_uses_default_port() -> None:
    parsed = parse_url("sftp://10.0.0.8/opt/playbooks")
    assert parsed["port"] == 22
    assert parsed["username"] is None
    assert canonical_url({**parsed, "username": "ops"}) == "sftp://ops@10.0.0.8/opt/playbooks"


def test_ipv6_host_is_bracketed_in_the_canonical_url() -> None:
    parsed = parse_url("sftp://[2001:db8::5]:2200/x")
    assert parsed["host"] == "2001:db8::5"
    assert canonical_url({**parsed, "username": "u"}) == "sftp://u@[2001:db8::5]:2200/x"


@pytest.mark.parametrize(
    "url",
    [
        "sftp://host/../etc",
        "sftp://host/a/../../b",
        "sftp://host:70000/x",
        "sftp://host",
        "https://host/x",
        "sftp://us er@host/x",
    ],
)
def test_bad_urls_are_refused(url: str) -> None:
    with pytest.raises(CatalogError) as exc:
        parse_url(url)
    assert exc.value.code == "invalid_sftp_url"
