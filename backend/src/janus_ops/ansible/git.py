"""Git sources: a shallow clone into a private temporary directory, read, then removed.

The working tree of a playbook is never used directly: the catalog stores the files as versions,
and runs read the catalog. The deploy key (an SSH key from the vault) lives only for the clone.
"""

import asyncio
import os
import re
import stat
import tempfile
from pathlib import Path

from janus_ops.ansible.catalog import CatalogError, scan_tree

CLONE_TIMEOUT_S = 300
REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
URL_PATTERNS = (
    re.compile(r"^https://[^\s@/]+(?:/[^\s]*)?$"),
    re.compile(r"^ssh://[^\s]+$"),
    re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+$"),
)


def validate_git_url(url: str) -> str:
    """https:// (no credentials in the URL), ssh:// or scp-style user@host:path only."""
    if not any(p.match(url) for p in URL_PATTERNS):
        raise CatalogError("invalid_git_url")
    if re.match(r"^https://[^/]*:[^/]*@", url):
        raise CatalogError("credentials_in_url")
    return url


def validate_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not REF_PATTERN.match(ref) or ".." in ref:
        raise CatalogError("invalid_ref")
    return ref


async def fetch(url: str, ref: str | None, deploy_key: str | None) -> dict[str, bytes]:
    """Files of the repository at `ref` (default branch when None)."""
    validate_git_url(url)
    validate_ref(ref)
    with tempfile.TemporaryDirectory(prefix="janus-git-") as tmp:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tmp,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        if deploy_key is not None:
            key_path = Path(tmp) / "deploy_key"
            fd = os.open(
                key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
            )
            with os.fdopen(fd, "w") as handle:
                handle.write(deploy_key if deploy_key.endswith("\n") else deploy_key + "\n")
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {key_path} -o IdentitiesOnly=yes -o BatchMode=yes"
                f" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile={tmp}/known_hosts"
            )
        target = Path(tmp) / "repo"
        command = ["git", "clone", "--quiet", "--depth", "1"]
        if ref:
            command += ["--branch", ref]
        command += ["--", url, str(target)]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=tmp,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(process.communicate(), CLONE_TIMEOUT_S)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise CatalogError("git_timeout") from None
        if process.returncode != 0:
            raise CatalogError("git_failed")
        return scan_tree(target)
