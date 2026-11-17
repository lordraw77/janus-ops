"""Moving the master key between the local key and an external secret manager (FR-86) on a real
database: credentials, workflow webhook secrets and TOTP seeds are re-wrapped, nothing is lost,
the move is idempotent, reversible and all-or-nothing. A fake OpenBao stands in for the manager."""

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import create_async_engine

from janus_ops import secrets_migrate
from janus_ops.crypto import SecretBox
from janus_ops.db import alembic_config
from janus_ops.transit import build_box
from janus_ops.vault import Vault, VaultUnavailable

from ._bao import ROLE_ID, SECRET_ID, FakeBao, fake_bao
from ._it import CONFIGURED, DB_URL, SECRET_KEY, run_sql, settings, signed_in

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not CONFIGURED, reason="test database / lldap not configured"),
]


@pytest.fixture(scope="module", autouse=True)
def migrated() -> Iterator[None]:
    cfg = alembic_config(DB_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


def _settings(bao: FakeBao, **kw: Any) -> Any:
    return settings(
        secret_backend="openbao-transit",
        bao_addr=bao.addr,
        bao_role_id=ROLE_ID,
        bao_secret_id=SECRET_ID,
        **kw,
    )


def _run(coro: Any) -> Any:
    async def go() -> Any:
        engine = create_async_engine(DB_URL)
        try:
            return await coro(engine)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _first_byte(table: str, column: str, row_id: str) -> int:
    rows = run_sql(
        f"SELECT get_byte({column}, 0) FROM {table} WHERE id = CAST(:i AS uuid)",  # noqa: S608
        {"i": row_id},
    )
    return int(rows[0][0])


def _credential(password: str) -> str:
    with signed_in("alice") as admin:
        r = admin.post(
            "/api/v1/credentials",
            json={
                "name": f"mig-{uuid.uuid4().hex[:8]}",
                "kind": "ssh_password",
                "username": "ops",
                "secret": password,
            },
        )
        assert r.status_code == 201, r.text
        return str(r.json()["id"])


def _reveal(box: Any, cred_id: str) -> str:
    row = run_sql(
        "SELECT ciphertext, nonce, dek_wrapped FROM credential WHERE id = CAST(:i AS uuid)",
        {"i": cred_id},
    )[0]
    data = Vault(box).reveal(cred_id, row[0], row[1], row[2])
    return str(data["secret"])


def test_a_round_trip_between_the_local_key_and_the_secret_manager() -> None:
    cred = _credential("hunter2-hunter2")
    user = str(run_sql("SELECT id FROM app_user ORDER BY username LIMIT 1")[0][0])
    seed_aad = secrets_migrate.totp_aad(user)
    run_sql(
        "UPDATE app_user SET totp_secret_enc = :e WHERE id = CAST(:i AS uuid)",
        {"e": SecretBox(SECRET_KEY).encrypt(b"JBSWY3DPEHPK3PXP", seed_aad), "i": user},
    )
    assert _first_byte("credential", "dek_wrapped", cred) == 1  # written by the local key

    with fake_bao() as bao:
        s = _settings(bao)
        to_transit = build_box(s, primary="openbao-transit")
        assert to_transit is not None

        first = _run(lambda e: secrets_migrate.migrate(e, to_transit, "openbao-transit"))
        assert first.moved["credential"] >= 1 and first.moved["app_user.totp"] == 1
        assert _first_byte("credential", "dek_wrapped", cred) == 0x54
        assert (
            int(
                run_sql(
                    "SELECT kek_version FROM credential WHERE id = CAST(:i AS uuid)", {"i": cred}
                )[0][0]
            )
            == 2
        )

        # a server on the secret manager reads it; the local key alone no longer can
        assert _reveal(build_box(s), cred) == "hunter2-hunter2"
        local_only = Vault(SecretBox(SECRET_KEY))
        row = run_sql(
            "SELECT ciphertext, nonce, dek_wrapped FROM credential WHERE id = CAST(:i AS uuid)",
            {"i": cred},
        )[0]
        with pytest.raises(Exception, match="format"):
            local_only.reveal(cred, row[0], row[1], row[2])
        seed = run_sql(
            "SELECT totp_secret_enc FROM app_user WHERE id = CAST(:i AS uuid)", {"i": user}
        )
        assert to_transit.decrypt(bytes(seed[0][0]), seed_aad) == b"JBSWY3DPEHPK3PXP"

        # idempotent: a second run finds everything already there
        second = _run(lambda e: secrets_migrate.migrate(e, to_transit, "openbao-transit"))
        assert second.total_moved == 0 and second.already["credential"] >= 1

        status = _run(secrets_migrate.status)
        assert status["credential"] == {"openbao-transit": first.moved["credential"]}

        # and back
        to_local = build_box(s, primary="local")
        assert to_local is not None
        back = _run(lambda e: secrets_migrate.migrate(e, to_local, "local"))
        assert back.moved["credential"] == first.moved["credential"]
        assert _first_byte("credential", "dek_wrapped", cred) == 1
        assert _reveal(SecretBox(SECRET_KEY), cred) == "hunter2-hunter2"

    audit = run_sql(
        "SELECT target_id FROM audit_event WHERE action = 'secrets.migrated' ORDER BY id"
    )
    assert [r[0] for r in audit] == ["openbao-transit", "openbao-transit", "local"]


def test_a_move_that_cannot_finish_changes_nothing() -> None:
    cred = _credential("another-secret-1")
    with fake_bao() as bao:
        box = build_box(_settings(bao), primary="openbao-transit")
        assert box is not None
        bao.sealed = True  # the secret manager goes away before the move
        with pytest.raises(VaultUnavailable):
            _run(lambda e: secrets_migrate.migrate(e, box, "openbao-transit"))
    assert _first_byte("credential", "dek_wrapped", cred) == 1
    assert _reveal(SecretBox(SECRET_KEY), cred) == "another-secret-1"


def test_a_server_on_the_secret_manager_uses_it_for_new_secrets() -> None:
    with (
        fake_bao() as bao,
        signed_in(
            "alice",
            secret_backend="openbao-transit",
            bao_addr=bao.addr,
            bao_role_id=ROLE_ID,
            bao_secret_id=SECRET_ID,
        ) as admin,
    ):
        r = admin.post(
            "/api/v1/credentials",
            json={
                "name": f"new-{uuid.uuid4().hex[:8]}",
                "kind": "ssh_password",
                "username": "ops",
                "secret": "written-through-transit",
            },
        )
        assert r.status_code == 201, r.text
        cred = str(r.json()["id"])
        assert _first_byte("credential", "dek_wrapped", cred) == 0x54
        assert bao.count("encrypt") >= 1
        ready = admin.get("/api/v1/ready")
        assert ready.json()["checks"]["secret_manager"] == "ok"
        bao.sealed = True
        down = admin.get("/api/v1/ready")
        assert down.status_code == 503 and down.json()["checks"]["secret_manager"] == "unreachable"
