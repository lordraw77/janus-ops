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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0

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
