"""In-process SSH server for terminal tests (asyncssh in a background thread).

The shell greets with user, terminal type and size, echoes input as `echo:<data>`, prints the
current size on `size`, and exits on `exit`. TCP forwarding is allowed (jump host tests).
SFTP serves `root` (a temporary directory) as `/`, with `/home/ops` as starting directory.
"""

import asyncio
import tempfile
import threading
from pathlib import Path
from typing import Any

import asyncssh

PASSWORD = "sshd-password-123"


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return password == PASSWORD

    def connection_requested(
        self, dest_host: str, dest_port: int, orig_host: str, orig_port: int
    ) -> bool:
        return True  # direct-tcpip: used when this server acts as jump host


async def _shell(process: asyncssh.SSHServerProcess[str]) -> None:
    width, height, _, _ = process.get_terminal_size()
    process.stdout.write(
        f"welcome {process.get_extra_info('username')} term={process.get_terminal_type()}"
        f" size={width}x{height}\r\n"
    )
    while True:
        try:
            data = await process.stdin.read(1024)
        except asyncssh.TerminalSizeChanged:
            continue
        if not data:
            break
        if "exit" in data:
            break
        if "size" in data:
            width, height, _, _ = process.get_terminal_size()
            process.stdout.write(f"size={width}x{height}\r\n")
            continue
        process.stdout.write(f"echo:{data}")
    process.exit(0)


class _ChrootSFTP(asyncssh.SFTPServer):
    def __init__(self, chan: asyncssh.SSHServerChannel[bytes], root: str) -> None:
        super().__init__(chan, chroot=root.encode())

    def realpath(self, path: bytes) -> bytes:
        if path in (b".", b""):
            path = b"/home/ops"
        return super().realpath(path)


class TestSSHD:
    """Start with .start(); `port`, `host_key` and `client_key` are then available."""

    __test__ = False  # not a pytest class

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="janus-sftp-")
        self.root = Path(self._tmp.name)
        (self.root / "home" / "ops").mkdir(parents=True)
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._server: Any = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def _start() -> None:
            authorized = asyncssh.import_authorized_keys(
                self.client_key.export_public_key().decode()
            )
            self._server = await asyncssh.create_server(
                _Server,
                "127.0.0.1",
                0,
                server_host_keys=[self.host_key],
                authorized_client_keys=authorized,
                process_factory=_shell,
                allow_pty=True,
                sftp_factory=lambda chan: _ChrootSFTP(chan, str(self.root)),
            )
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()

        self._loop.run_until_complete(_start())
        self._loop.run_forever()

    def start(self) -> "TestSSHD":
        self._thread.start()
        if not self._ready.wait(10):
            raise RuntimeError("test sshd did not start")
        return self

    def stop(self) -> None:
        def _close() -> None:
            self._server.close()
            self._loop.stop()

        self._loop.call_soon_threadsafe(_close)
        self._thread.join(5)
        self._tmp.cleanup()

    @property
    def client_key_openssh(self) -> str:
        return bytes(self.client_key.export_private_key("openssh")).decode()
