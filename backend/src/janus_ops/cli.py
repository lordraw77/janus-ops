"""`janus-ops` command line: serve | migrate | version."""

import argparse
import logging
import sys

from pydantic import ValidationError

from janus_ops import __version__

log = logging.getLogger("janus_ops")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="janus-ops", description="Janus-Ops control plane")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the HTTP server (API + web UI)")
    sub.add_parser("migrate", help="apply database migrations up to the latest revision")
    sub.add_parser(
        "setup",
        help="migrate, then create the initial administrator from JANUS_ADMIN_* if missing",
    )
    sub.add_parser("version", help="print the version and exit")
    kg = sub.add_parser(
        "agent-keygen",
        help="generate the agent release key pair (run it OFF the server; keep the private key)",
    )
    kg.add_argument("--private-key", required=True, help="where to write the private key (0600)")
    sg = sub.add_parser(
        "agent-sign", help="sign the agent binaries in a directory with the release private key"
    )
    sg.add_argument("--private-key", required=True)
    sg.add_argument("--dir", required=True, help="directory with janus-agent-linux-<arch>")
    sg.add_argument("--agent-version", required=True, help="version of these binaries, e.g. 0.3.0")
    bg = sub.add_parser(
        "breakglass", help="create the break-glass admin account or rotate its password"
    )
    bg.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from standard input instead of prompting",
    )
    return parser


def _read_password(from_stdin: bool) -> str | None:
    import getpass

    from janus_ops.auth.passwords import MIN_LENGTH

    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass(f"Break-glass password (min {MIN_LENGTH} chars): ")
        if getpass.getpass("Repeat: ") != password:
            print("janus-ops: passwords do not match", file=sys.stderr)
            return None
    if len(password) < MIN_LENGTH:
        print(f"janus-ops: password must be at least {MIN_LENGTH} characters", file=sys.stderr)
        return None
    return password


def _release_command(args: argparse.Namespace) -> int:
    """Release key handling: needs no server configuration and no secrets of the server."""
    import base64
    import json
    import os
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from janus_ops.agents import update

    key_path = Path(args.private_key)
    if args.command == "agent-keygen":
        if key_path.exists():
            print(f"janus-ops: {key_path} already exists", file=sys.stderr)
            return 2
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        print("Public key (JANUS_AGENT_RELEASE_PUBLIC_KEY on the server):")
        print(base64.b64encode(public).decode())
        return 0

    if not update.VERSION_RE.match(args.agent_version):
        print("janus-ops: --agent-version must look like 0.3.0", file=sys.stderr)
        return 2
    try:
        loaded = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError):
        print("janus-ops: cannot read the private key", file=sys.stderr)
        return 2
    if not isinstance(loaded, Ed25519PrivateKey):
        print("janus-ops: the key is not an ed25519 key", file=sys.stderr)
        return 2
    signed = 0
    for arch in update.ARCHES:
        binary = Path(args.dir) / update.BINARY.format(arch=arch)
        if not binary.is_file():
            continue
        meta = update.sign(loaded, args.agent_version, arch, binary.read_bytes())
        binary.with_name(binary.name + ".sig").write_text(json.dumps(meta, indent=2) + "\n")
        print(f"signed {binary.name} {args.agent_version} sha256={meta['sha256'][:16]}…")
        signed += 1
    if not signed:
        print("janus-ops: no agent binaries found", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0

    if args.command in ("agent-keygen", "agent-sign"):
        return _release_command(args)

    # Imported lazily so `version` works without any configuration
    from janus_ops.config import get_settings
    from janus_ops.logsetup import configure_logging

    try:
        settings = get_settings()
    except ValidationError as exc:
        # Report which variables are wrong, never their values
        problems = [
            "JANUS_" + str(err["loc"][0]).upper() if err["loc"] else str(err["msg"])
            for err in exc.errors()
        ]
        print(f"janus-ops: invalid configuration: {'; '.join(problems)}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level)

    if args.command in ("migrate", "setup"):
        from alembic import command

        from janus_ops.db import alembic_config

        log.info("applying migrations")
        command.upgrade(alembic_config(settings.database_url.get_secret_value()), "head")
        log.info("migrations applied")
        if args.command == "migrate":
            return 0

        import asyncio

        from janus_ops.directory import DirectoryError
        from janus_ops.setup import SetupError, ensure_admin

        try:
            asyncio.run(ensure_admin(settings))
        except SetupError as exc:
            print(f"janus-ops: {exc}", file=sys.stderr)
            return 2
        except DirectoryError as exc:
            log.error("initial administrator not created", extra={"error": exc.code})
            return 1
        return 0

    if args.command == "breakglass":
        import asyncio

        from janus_ops.auth.breakglass import BreakglassError, set_password

        password = _read_password(args.password_stdin)
        if password is None:
            return 1
        try:
            outcome = asyncio.run(set_password(settings, password))
        except BreakglassError as exc:
            print(f"janus-ops: {exc}", file=sys.stderr)
            return 1
        print(f"break-glass account '{settings.breakglass_username}' {outcome}")
        return 0

    import uvicorn

    from janus_ops.main import create_app

    log.info("starting", extra={"version": __version__, "port": settings.port})
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,  # client IPs are personal data; privileged actions go to the audit log
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
