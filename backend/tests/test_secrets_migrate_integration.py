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
from janus_ops.transit import build_box, build_kv
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
    values: dict[str, Any] = {
        "secret_backend": "openbao-transit",
        "bao_addr": bao.addr,
        "bao_role_id": ROLE_ID,
        "bao_secret_id": SECRET_ID,
    }
    values.update(kw)
    return settings(**values)


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


# ---------------------------------------------------------------- KV (ADR-011 option b)
def _kv_settings(bao: FakeBao) -> Any:
    return _settings(bao, secret_backend="openbao-kv")


def _columns(cred_id: str) -> tuple[bytes, bytes, bytes, int]:
    row = run_sql(
        "SELECT ciphertext, nonce, dek_wrapped, kek_version FROM credential"
        " WHERE id = CAST(:i AS uuid)",
        {"i": cred_id},
    )[0]
    return bytes(row[0]), bytes(row[1]), bytes(row[2]), int(row[3])


def test_secrets_move_to_kv_and_back_without_losing_anything() -> None:
    cred = _credential("kv-journey-secret")
    with fake_bao() as bao:
        s = _kv_settings(bao)
        box, kv = build_box(s, primary="openbao-transit"), build_kv(s)
        assert box is not None and kv is not None

        moved = _run(lambda e: secrets_migrate.migrate(e, box, "openbao-kv", kv))
        assert moved.moved["credential"] >= 1
        ct, nonce, dek, version = _columns(cred)
        assert ct.startswith(b"kv1:") and nonce == b"" and dek == b"" and version == 3
        assert b"kv-journey-secret" not in ct
        assert bao.kv[f"janus-ops/{cred}"] == [{"secret": "kv-journey-secret"}]
        assert Vault(box, kv).reveal(cred, ct, nonce, dek) == {"secret": "kv-journey-secret"}
        counts = _run(secrets_migrate.status)
        assert counts["credential"].get("openbao-kv", 0) >= 1

        again = _run(lambda e: secrets_migrate.migrate(e, box, "openbao-kv", kv))
        assert again.total_moved == 0

        # back to the local key: the content returns to the database, KV is emptied of it
        to_local = build_box(s, primary="local")
        assert to_local is not None
        back = _run(lambda e: secrets_migrate.migrate(e, to_local, "local", kv))
        assert back.moved["credential"] == moved.moved["credential"]
        assert not back.left_in_kv
        assert f"janus-ops/{cred}" not in bao.kv
        assert _first_byte("credential", "dek_wrapped", cred) == 1
        assert _reveal(SecretBox(SECRET_KEY), cred) == "kv-journey-secret"


def test_a_failed_move_to_kv_leaves_the_database_alone() -> None:
    cred = _credential("never-moves")
    with fake_bao() as bao:
        s = _kv_settings(bao)
        box, kv = build_box(s, primary="openbao-transit"), build_kv(s)
        assert box is not None and kv is not None
        bao.sealed = True
        with pytest.raises(VaultUnavailable):
            _run(lambda e: secrets_migrate.migrate(e, box, "openbao-kv", kv))
    assert _first_byte("credential", "dek_wrapped", cred) == 1
    assert _reveal(SecretBox(SECRET_KEY), cred) == "never-moves"


def test_a_server_on_kv_stores_new_secrets_there_and_deleting_removes_them() -> None:
    with (
        fake_bao() as bao,
        signed_in(
            "alice",
            secret_backend="openbao-kv",
            bao_addr=bao.addr,
            bao_role_id=ROLE_ID,
            bao_secret_id=SECRET_ID,
        ) as admin,
    ):
        r = admin.post(
            "/api/v1/credentials",
            json={
                "name": f"kv-{uuid.uuid4().hex[:8]}",
                "kind": "ssh_password",
                "username": "ops",
                "secret": "straight-to-kv",
            },
        )
        assert r.status_code == 201, r.text
        cred = str(r.json()["id"])
        ct, _nonce, dek, version = _columns(cred)
        assert ct.startswith(b"kv1:") and dek == b"" and version == 3
        assert bao.kv[f"janus-ops/{cred}"] == [{"secret": "straight-to-kv"}]
        # changing the secret writes a new version and the row points at it
        up = admin.put(f"/api/v1/credentials/{cred}/secret", json={"secret": "second"})
        assert up.status_code == 204, up.text
        assert len(bao.kv[f"janus-ops/{cred}"]) == 2
        # deleting the credential takes its content out of the secret manager
        assert admin.delete(f"/api/v1/credentials/{cred}").status_code == 204
        assert f"janus-ops/{cred}" not in bao.kv


def test_prune_finds_the_secrets_nothing_refers_to() -> None:
    cred = _credential("referenced")
    with fake_bao() as bao:
        s = _kv_settings(bao)
        box, kv = build_box(s, primary="openbao-transit"), build_kv(s)
        assert box is not None and kv is not None
        _run(lambda e: secrets_migrate.migrate(e, box, "openbao-kv", kv))
        Vault(box, kv).seal("00000000-0000-0000-0000-00000000dead", {"secret": "orphan"})
        found = _run(lambda e: secrets_migrate.orphans(e, kv))
        assert found == {"00000000-0000-0000-0000-00000000dead"}
        assert cred in kv.entities()  # the referenced one is not an orphan
        # leave the database as found: this fake secret manager goes away with the test
        local = build_box(s, primary="local")
        assert local is not None
        _run(lambda e: secrets_migrate.migrate(e, local, "local", kv))


def _channel_with_secret(secret: str) -> str:
    """A channel whose secret is sealed the way the API does it: under "channel-<id>"."""
    from janus_ops.monitoring.notify import channel_secret_id

    cid = str(uuid.uuid4())
    sealed = Vault(SecretBox(SECRET_KEY)).seal(channel_secret_id(cid), {"token": secret})
    run_sql(
        "INSERT INTO notification_channel (id, name, kind, config, enabled, secret_ciphertext,"
        " secret_nonce, secret_dek, kek_version) VALUES (CAST(:id AS uuid), :n, 'webhook',"
        " CAST(:c AS jsonb), true, :ct, :nonce, :dek, 1)",
        {
            "id": cid,
            "n": f"chan-{cid[:8]}",
            "c": '{"url": "https://example.invalid/hook"}',
            "ct": sealed.ciphertext,
            "nonce": sealed.nonce,
            "dek": sealed.dek_wrapped,
        },
    )
    return cid


def _channel_row(cid: str) -> tuple[bytes, bytes, bytes]:
    row = run_sql(
        "SELECT secret_ciphertext, secret_nonce, secret_dek FROM notification_channel"
        " WHERE id = CAST(:i AS uuid)",
        {"i": cid},
    )[0]
    return bytes(row[0]), bytes(row[1]), bytes(row[2])


def test_channel_secrets_move_under_their_own_namespace() -> None:
    from janus_ops.monitoring.notify import channel_secret_id

    cid = _channel_with_secret("bot-token-123")
    vault_id = channel_secret_id(cid)
    with fake_bao() as bao:
        s = _kv_settings(bao)
        transit, kv = build_box(s, primary="openbao-transit"), build_kv(s)
        assert transit is not None and kv is not None

        _run(lambda e: secrets_migrate.migrate(e, transit, "openbao-transit", kv))
        assert Vault(transit, kv).reveal(vault_id, *_channel_row(cid)) == {"token": "bot-token-123"}

        _run(lambda e: secrets_migrate.migrate(e, transit, "openbao-kv", kv))
        assert bao.kv[f"janus-ops/{vault_id}"] == [{"token": "bot-token-123"}]
        assert vault_id in _run(secrets_migrate.referenced_ids)  # not an orphan for prune

        local = build_box(s, primary="local")
        assert local is not None
        _run(lambda e: secrets_migrate.migrate(e, local, "local", kv))
        assert f"janus-ops/{vault_id}" not in bao.kv
        assert Vault(SecretBox(SECRET_KEY)).reveal(vault_id, *_channel_row(cid)) == {
            "token": "bot-token-123"
        }
