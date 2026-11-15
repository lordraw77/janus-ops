"""Pure rules of the playbook catalog: paths, archives, directory scans, hashes and diffs.

Nothing here touches the database or the network. Every function that reads untrusted input
(an uploaded archive, a mounted directory, a git checkout) rejects what it does not understand
instead of guessing: absolute paths, `..`, links, device files, binary or oversized content.
"""

import difflib
import hashlib
import io
import os
import posixpath
import stat
import tarfile
import zipfile
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024
MAX_TREE_FILES = 2000
MAX_TREE_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_PATH_LENGTH = 1024
PLAYBOOK_SUFFIXES = (".yml", ".yaml")
SKIPPED_DIRS = frozenset({".git", ".hg", ".svn"})


class CatalogError(ValueError):
    """Input rejected; `code` is the stable error shown to the API client."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_rel_path(raw: str) -> str:
    """A relative, normalised POSIX path inside a source. Anything else is refused."""
    if not raw or len(raw) > MAX_PATH_LENGTH or "\x00" in raw or "\\" in raw:
        raise CatalogError("invalid_rel_path")
    if raw.startswith("/"):
        raise CatalogError("invalid_rel_path")
    parts = raw.split("/")
    if any(part in ("", ".", "..") for part in parts) or parts[0] in SKIPPED_DIRS:
        raise CatalogError("invalid_rel_path")
    if any(part in SKIPPED_DIRS for part in parts):
        raise CatalogError("invalid_rel_path")
    return "/".join(parts)


def is_playbook(path: str) -> bool:
    return path.lower().endswith(PLAYBOOK_SUFFIXES)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_text(data: bytes) -> str:
    """UTF-8 text without NUL bytes; binary files are not part of the catalog."""
    if len(data) > MAX_FILE_BYTES:
        raise CatalogError("file_too_large")
    if b"\x00" in data:
        raise CatalogError("binary_file")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CatalogError("binary_file") from None


def _strip_common_root(files: dict[str, bytes]) -> dict[str, bytes]:
    """Archives made as `dir/...` lose the top folder when it is the only one."""
    tops = {path.split("/", 1)[0] for path in files}
    if len(tops) == 1 and all("/" in path for path in files):
        prefix = tops.pop() + "/"
        return {path[len(prefix) :]: data for path, data in files.items()}
    return files


def _collect(entries: list[tuple[str, bytes]]) -> dict[str, bytes]:
    if len(entries) > MAX_TREE_FILES:
        raise CatalogError("too_many_files")
    total = 0
    files: dict[str, bytes] = {}
    for raw_path, data in entries:
        path = validate_rel_path(raw_path.rstrip("/"))
        total += len(data)
        if total > MAX_TREE_BYTES:
            raise CatalogError("archive_too_large")
        if path in files:
            raise CatalogError("duplicate_path")
        files[path] = data
    return _strip_common_root(files)


def _tar_entries(tar: tarfile.TarFile) -> list[tuple[str, bytes]]:
    entries: list[tuple[str, bytes]] = []
    for member in tar.getmembers():
        if member.isdir():
            continue
        if not member.isreg():
            raise CatalogError("special_file_in_archive")
        if member.size > MAX_FILE_BYTES:
            raise CatalogError("file_too_large")
        handle = tar.extractfile(member)
        if handle is None:
            raise CatalogError("bad_archive")
        entries.append((member.name, handle.read()))
    return entries


def unpack_archive(filename: str, data: bytes) -> dict[str, bytes]:
    """Files of a .zip, .tar.gz or .tgz upload. Links, devices and absolute paths are rejected."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise CatalogError("upload_too_large")
    name = filename.lower()
    entries: list[tuple[str, bytes]] = []
    if name.endswith(".zip"):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise CatalogError("bad_archive") from None
        with archive:
            for info in archive.infolist():
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise CatalogError("symlink_in_archive")
                if info.is_dir():
                    continue
                # Permission-only modes (no file type bits, as Python's zipfile writes) are files
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise CatalogError("special_file_in_archive")
                if info.file_size > MAX_FILE_BYTES:
                    raise CatalogError("file_too_large")
                entries.append((info.filename, archive.read(info)))
    elif name.endswith((".tar.gz", ".tgz")):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                entries = _tar_entries(tar)
        except (tarfile.TarError, OSError):
            raise CatalogError("bad_archive") from None
    else:
        raise CatalogError("unsupported_archive")
    return _collect(entries)


def scan_tree(root: Path) -> dict[str, bytes]:
    """Files under a directory that is already mounted on this host.

    Symbolic links are skipped rather than followed, so nothing outside `root` is read.
    """
    if not root.is_absolute() or not root.is_dir():
        raise CatalogError("mount_not_found")
    real_root = os.path.realpath(root)
    entries: list[tuple[str, bytes]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(real_root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full):
                continue
            info = os.lstat(full)
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > MAX_FILE_BYTES:
                continue
            total += info.st_size
            if total > MAX_TREE_BYTES:
                raise CatalogError("tree_too_large")
            rel = posixpath.normpath(os.path.relpath(full, real_root).replace(os.sep, "/"))
            with open(full, "rb") as handle:
                entries.append((rel, handle.read()))
            if len(entries) > MAX_TREE_FILES:
                raise CatalogError("too_many_files")
    return {path: data for path, data in entries}


def unified_diff(old: str, new: str, old_label: str, new_label: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=old_label,
            tofile=new_label,
        )
    )
