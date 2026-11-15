"""F6 unit tests: catalog rules, archive safety, git URL checks, runner planning helpers.

No database and no ansible binary: the integration path (sources in PostgreSQL, a real run)
is covered by the integration suite when JANUS_TEST_DATABASE_URL is set.
"""

import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from janus_ops.ansible import git, runner
from janus_ops.ansible.catalog import (
    MAX_FILE_BYTES,
    CatalogError,
    decode_text,
    is_playbook,
    scan_tree,
    sha256_hex,
    unified_diff,
    unpack_archive,
    validate_rel_path,
)
from janus_ops.ansible.router import _mount_allowed
from janus_ops.config import Settings


def _code(exc: pytest.ExceptionInfo[CatalogError]) -> str:
    return exc.value.code


# ------------------------------------------------------------------ paths


@pytest.mark.parametrize(
    "raw",
    ["site.yml", "roles/web/tasks/main.yml", "a/b/c.yaml"],
)
def test_valid_relative_paths_are_kept(raw: str) -> None:
    assert validate_rel_path(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "/etc/passwd",
        "../secret.yml",
        "a/../b.yml",
        "a/./b.yml",
        "a//b.yml",
        "a\\b.yml",
        "bad\x00.yml",
        ".git/config",
        "roles/.git/HEAD",
        "x" * 1025,
    ],
)
def test_unsafe_paths_are_refused(raw: str) -> None:
    with pytest.raises(CatalogError) as exc:
        validate_rel_path(raw)
    assert _code(exc) == "invalid_rel_path"


def test_playbook_detection_is_by_yaml_suffix() -> None:
    assert is_playbook("site.yml")
    assert is_playbook("Site.YAML")
    assert not is_playbook("roles/web/defaults.txt")


def test_text_decoding_refuses_binary_and_oversized() -> None:
    assert decode_text(b"ciao\n") == "ciao\n"
    with pytest.raises(CatalogError) as binary:
        decode_text(b"\x89PNG\x00\x00")
    assert _code(binary) == "binary_file"
    with pytest.raises(CatalogError) as invalid:
        decode_text(b"\xff\xfe\xfd")
    assert _code(invalid) == "binary_file"
    with pytest.raises(CatalogError) as big:
        decode_text(b"a" * (MAX_FILE_BYTES + 1))
    assert _code(big) == "file_too_large"


def test_sha256_and_diff() -> None:
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    diff = unified_diff("a\nb\n", "a\nc\n", "x@1", "x@2")
    assert "-b" in diff and "+c" in diff
    assert diff.startswith("--- x@1")


# --------------------------------------------------------------- archives


def _zip(entries: list[tuple[str, bytes]], symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/shadow")
    return buffer.getvalue()


def _tgz(entries: dict[str, bytes], link: tuple[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link is not None:
            info = tarfile.TarInfo(link[0])
            info.type = tarfile.SYMTYPE
            info.linkname = link[1]
            tar.addfile(info)
    return buffer.getvalue()


def test_zip_files_are_read_and_common_root_is_stripped() -> None:
    data = _zip([("play/site.yml", b"- hosts: all\n"), ("play/roles/x/tasks/main.yml", b"[]\n")])
    files = unpack_archive("play.zip", data)
    assert files == {"site.yml": b"- hosts: all\n", "roles/x/tasks/main.yml": b"[]\n"}


def test_tgz_files_are_read() -> None:
    files = unpack_archive("play.tar.gz", _tgz({"site.yml": b"- hosts: all\n"}))
    assert files == {"site.yml": b"- hosts: all\n"}


def test_zip_symlink_is_refused() -> None:
    with pytest.raises(CatalogError) as exc:
        unpack_archive("bad.zip", _zip([("a.yml", b"x")], symlink="link.yml"))
    assert _code(exc) == "symlink_in_archive"


def test_tar_symlink_is_refused() -> None:
    with pytest.raises(CatalogError) as exc:
        unpack_archive("bad.tgz", _tgz({"a.yml": b"x"}, link=("link.yml", "/etc/shadow")))
    assert _code(exc) == "special_file_in_archive"


def test_unknown_or_broken_archives_are_refused() -> None:
    with pytest.raises(CatalogError) as unknown:
        unpack_archive("play.rar", b"whatever")
    assert _code(unknown) == "unsupported_archive"
    with pytest.raises(CatalogError) as broken:
        unpack_archive("play.zip", b"not a zip")
    assert _code(broken) == "bad_archive"


def test_archive_with_absolute_path_is_refused() -> None:
    with pytest.raises(CatalogError) as exc:
        unpack_archive("p.zip", _zip([("/etc/cron.d/x.yml", b"x")]))
    assert _code(exc) == "invalid_rel_path"


# ------------------------------------------------------------ directories


def test_scan_tree_skips_symlinks_and_git(tmp_path: Path) -> None:
    root = tmp_path / "share"
    (root / "roles").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "site.yml").write_text("- hosts: all\n")
    (root / "roles" / "main.yml").write_text("[]\n")
    (root / ".git" / "config").write_text("[core]\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, root / "leak.yml")
    files = scan_tree(root)
    assert set(files) == {"site.yml", "roles/main.yml"}


def test_scan_tree_requires_an_existing_absolute_directory(tmp_path: Path) -> None:
    with pytest.raises(CatalogError) as relative:
        scan_tree(Path("relative/dir"))
    assert relative.value.code == "mount_not_found"
    with pytest.raises(CatalogError) as missing:
        scan_tree(tmp_path / "nope")
    assert missing.value.code == "mount_not_found"


def test_mount_must_be_inside_an_allowed_root(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    (allowed / "pb").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    os.symlink(other, allowed / "escape")
    roots = [allowed]
    assert _mount_allowed(str(allowed / "pb"), roots) == (allowed / "pb").resolve()
    for raw in (str(other), str(allowed / "escape"), "relative", str(tmp_path)):
        with pytest.raises(CatalogError) as exc:
            _mount_allowed(raw, roots)
        assert exc.value.code == "mount_not_allowed"
    with pytest.raises(CatalogError):
        _mount_allowed(str(allowed), [])


def test_network_sources_are_disabled_by_default() -> None:
    assert (
        Settings(_env_file=None, database_url="postgresql+asyncpg://u:p@localhost/db").ansible_roots
        == []
    )


# -------------------------------------------------------------------- git


@pytest.mark.parametrize(
    "url",
    [
        "https://git.example.org/ops/playbooks.git",
        "ssh://git@git.example.org/ops/playbooks.git",
        "git@git.example.org:ops/playbooks.git",
    ],
)
def test_git_urls_accepted(url: str) -> None:
    assert git.validate_git_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc",
        "ext::sh -c id",
        "--upload-pack=touch /tmp/x",
        "https://user:secret@git.example.org/x.git",
        "http://git.example.org/x.git",
        "https://git.example.org/x.git --config",
    ],
)
def test_git_urls_refused(url: str) -> None:
    with pytest.raises(CatalogError):
        git.validate_git_url(url)


def test_git_refs() -> None:
    assert git.validate_ref(None) is None
    assert git.validate_ref("release/1.2") == "release/1.2"
    for bad in ("-x", "a..b", "a b", "$(id)"):
        with pytest.raises(CatalogError):
            git.validate_ref(bad)


# ----------------------------------------------------------------- runner


def _node(name: str, port: int = 22) -> runner.Runnable:
    return runner.Runnable(
        node_id="0b5f0c1e-6a3e-4e8c-9d2a-1f2e3d4c5b6a",
        name=name,
        address="10.0.0.5",
        port=port,
        username="ansible",
        credential_id="c",
        host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIT",
    )


def test_inventory_uses_node_id_and_pinned_known_hosts(tmp_path: Path) -> None:
    node = _node("web-01")
    keys = {node.node_id: tmp_path / "key"}
    known = tmp_path / "known_hosts"
    inv = json.loads(runner.inventory([node], keys, {}, known))
    host = inv["all"]["hosts"]["h_0b5f0c1e6a3e4e8c9d2a1f2e3d4c5b6a"]
    assert host["ansible_host"] == "10.0.0.5"
    assert host["ansible_user"] == "ansible"
    assert host["ansible_ssh_private_key_file"] == str(keys[node.node_id])
    assert "StrictHostKeyChecking=yes" in host["ansible_ssh_common_args"]
    assert "web-01" not in json.dumps(inv)  # names are never used as inventory hosts


def test_known_hosts_brackets_non_default_ports() -> None:
    text = runner.known_hosts_text([_node("a"), _node("b", port=2222)])
    assert "10.0.0.5 ssh-ed25519" in text
    assert "[10.0.0.5]:2222 ssh-ed25519" in text


def test_materialise_refuses_escaping_paths(tmp_path: Path) -> None:
    runner.materialise(tmp_path, {"site.yml": "- hosts: all\n", "roles/x/main.yml": "[]\n"})
    assert (tmp_path / "roles" / "x" / "main.yml").read_text() == "[]\n"
    with pytest.raises(CatalogError):
        runner.materialise(tmp_path, {"../evil.yml": "x"})
    assert not (tmp_path.parent / "evil.yml").exists()


def test_key_files_are_private(tmp_path: Path) -> None:
    target = tmp_path / "key"
    runner.write_private(target, "secret\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_skip_reasons_for_unusable_nodes() -> None:
    base = dict(
        enabled=True,
        config={},
        credential_id="c",
        cred_kind="ssh_key",
        cred_user="root",
        host_key="ssh-ed25519 AAAA",
    )
    row = SimpleNamespace(**{**base, "name": "x"})
    assert runner._skip_reason(row) is None
    cases = {
        "no_ssh_capability": {"enabled": None},
        "ssh_disabled": {"enabled": False},
        "ssh_no_credential": {"credential_id": None},
        "unsupported_credential_kind": {"cred_kind": "generic"},
        "host_key_not_pinned": {"host_key": None},
    }
    for reason, change in cases.items():
        assert runner._skip_reason(SimpleNamespace(**{**base, **change})) == reason


def _jump_row(**change: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        id="j-1",
        name="bastion",
        address="203.0.113.7",
        enabled=True,
        config={"port": 2200, "username": "jump"},
        credential_id="jc",
        cred_kind="ssh_key",
        cred_user="root",
        host_key="ssh-ed25519 AAAAJUMP",
    )
    return SimpleNamespace(**{**base, **change})


def test_jump_host_is_planned_when_it_is_a_pinned_key_node() -> None:
    node = SimpleNamespace(id="n-1", name="app", config={"jump_host_id": "j-1"})
    reason, jump = runner._plan_jump(node, {"j-1": _jump_row()})
    assert reason is None
    assert jump == runner.JumpHost(
        node_id="j-1",
        name="bastion",
        address="203.0.113.7",
        port=2200,
        username="jump",
        credential_id="jc",
        host_key="ssh-ed25519 AAAAJUMP",
    )


@pytest.mark.parametrize(
    ("jump", "reason"),
    [
        (None, "jump_host_not_found"),
        (_jump_row(enabled=None), "jump_host_ssh_disabled"),
        (_jump_row(enabled=False), "jump_host_ssh_disabled"),
        (_jump_row(cred_kind="ssh_password"), "jump_host_needs_key"),
        (_jump_row(config={"jump_host_id": "x"}), "jump_host_chained"),
        (_jump_row(config={}, cred_user=None), "jump_host_no_username"),
        (_jump_row(host_key=None), "jump_host_not_pinned"),
    ],
)
def test_jump_host_refusals(jump: SimpleNamespace | None, reason: str) -> None:
    node = SimpleNamespace(id="n-1", name="app", config={"jump_host_id": "j-1"})
    planned, _ = runner._plan_jump(node, {} if jump is None else {"j-1": jump})
    assert planned == reason


def test_node_without_jump_host_has_no_hop() -> None:
    node = SimpleNamespace(id="n-1", name="app", config={})
    assert runner._plan_jump(node, {}) == (None, None)


def test_inventory_routes_through_jump_host_with_its_own_key(tmp_path: Path) -> None:
    jump = runner.JumpHost(
        node_id="j-1",
        name="bastion",
        address="203.0.113.7",
        port=2200,
        username="jump",
        credential_id="jc",
        host_key="ssh-ed25519 AAAAJUMP",
    )
    node = runner.Runnable(
        node_id="n-1",
        name="app",
        address="10.0.0.9",
        port=22,
        username="root",
        credential_id="c",
        host_key="ssh-ed25519 AAAAAPP",
        jump=jump,
    )
    key = tmp_path / "node_key"
    jump_key = tmp_path / "jump_key"
    kh = tmp_path / "kh"
    inv = json.loads(runner.inventory([node], {"n-1": key}, {}, kh, {"j-1": jump_key}))
    common = inv["all"]["hosts"]["h_n1"]["ansible_ssh_common_args"]
    assert f"-i {jump_key} -p 2200" in common
    assert "-W %h:%p jump@203.0.113.7" in common
    assert f"UserKnownHostsFile={kh}" in common
    kh_text = runner.known_hosts_text([node, jump])
    assert "[203.0.113.7]:2200 ssh-ed25519 AAAAJUMP" in kh_text
    assert "10.0.0.9 ssh-ed25519 AAAAAPP" in kh_text


def test_inventory_refuses_a_jump_without_key(tmp_path: Path) -> None:
    jump = runner.JumpHost("j-1", "b", "203.0.113.7", 22, "jump", "jc", "ssh-ed25519 A")
    node = runner.Runnable("n-1", "app", "10.0.0.9", 22, "root", "c", "ssh-ed25519 B", jump)
    with pytest.raises(ValueError):
        runner.inventory([node], {"n-1": tmp_path / "k"}, {}, tmp_path / "kh", {})


def test_password_node_inventory_uses_password_not_batchmode(tmp_path: Path) -> None:
    key_node = _node("key-host")
    pw_node = runner.Runnable(
        node_id="1c6f0c1e-6a3e-4e8c-9d2a-1f2e3d4c5b6b",
        name="pw-host",
        address="10.0.0.6",
        port=22,
        username="root",
        credential_id="p",
        host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIT",
    )
    keys = {key_node.node_id: tmp_path / "key"}
    inv = json.loads(
        runner.inventory([key_node, pw_node], keys, {pw_node.node_id: "s3cret"}, tmp_path / "kh")
    )
    key_host = inv["all"]["hosts"]["h_0b5f0c1e6a3e4e8c9d2a1f2e3d4c5b6a"]
    pw_host = inv["all"]["hosts"]["h_1c6f0c1e6a3e4e8c9d2a1f2e3d4c5b6b"]
    assert key_host["ansible_ssh_private_key_file"] == str(keys[key_node.node_id])
    assert "BatchMode=yes" in key_host["ansible_ssh_common_args"]
    assert "ansible_password" not in key_host
    assert pw_host["ansible_password"] == "s3cret"
    assert "BatchMode" not in pw_host["ansible_ssh_common_args"]
    assert "ansible_ssh_private_key_file" not in pw_host
