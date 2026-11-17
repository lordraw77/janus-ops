"""Move the master key between backends (FR-86, ADR-011): `janus-ops secrets migrate --to ...`.

Only the small data keys (DEKs) are re-wrapped: they are decrypted with whichever backend wrote
them and encrypted again with the target. The secrets stay as they are in the database, so the
move is quick, idempotent (rows already on the target are left alone), and reversible with the
same command. TOTP seeds are small enough to be encrypted again whole. One transaction: either
every row moves or none does. Every move is written to the audit log (counts only).
"""

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops import audit
from janus_ops.crypto import Box

# table, id column, data key column, version column (None: no version column)
DEK_TABLES = (
    ("credential", "id", "dek_wrapped", "kek_version"),
    ("notification_channel", "id", "secret_dek", "kek_version"),
    ("gitops_stack", "id", "secret_dek", "secret_kek_version"),
    ("workflow", "id", "secret_dek", "secret_kek_version"),
)
PREFIX = {"local": 1, "openbao-transit": 0x54}


@dataclass
class Report:
    moved: dict[str, int] = field(default_factory=dict)
    already: dict[str, int] = field(default_factory=dict)

    @property
    def total_moved(self) -> int:
        return sum(self.moved.values())


def dek_aad(entity_id: str) -> bytes:
    """Same binding as Vault.seal: the wrapped data key belongs to its row."""
    return f"credential:{entity_id}:dek".encode()


def totp_aad(user_id: str) -> bytes:
    return f"totp:{user_id}".encode()


async def status(engine: AsyncEngine) -> dict[str, dict[str, int]]:
    """How many wrapped keys each table has per backend."""
    out: dict[str, dict[str, int]] = {}
    async with engine.connect() as conn:
        for table, _id, col, _ver in DEK_TABLES:
            rows = await conn.execute(
                text(
                    f"SELECT CASE get_byte({col}, 0) WHEN 1 THEN 'local' WHEN 84 THEN"  # noqa: S608
                    f" 'openbao-transit' ELSE 'unknown' END AS backend, count(*) AS n"
                    f" FROM {table} WHERE {col} IS NOT NULL GROUP BY 1"
                )
            )
            out[table] = {r.backend: int(r.n) for r in rows}
        rows = await conn.execute(
            text(
                "SELECT CASE get_byte(totp_secret_enc, 0) WHEN 1 THEN 'local' WHEN 84 THEN"
                " 'openbao-transit' ELSE 'unknown' END AS backend, count(*) AS n"
                " FROM app_user WHERE totp_secret_enc IS NOT NULL GROUP BY 1"
            )
        )
        out["app_user.totp"] = {r.backend: int(r.n) for r in rows}
    return out


async def migrate(engine: AsyncEngine, box: Box, target: str, actor: str = "cli") -> Report:
    """Re-wrap every data key (and TOTP seed) that is not yet on `target`."""
    want = PREFIX[target]
    report = Report()
    async with engine.begin() as conn:
        for table, id_col, col, ver in DEK_TABLES:
            rows = (
                await conn.execute(
                    text(
                        f"SELECT {id_col}::text AS id, {col} AS blob FROM {table}"  # noqa: S608
                        f" WHERE {col} IS NOT NULL FOR UPDATE"
                    )
                )
            ).all()
            for row in rows:
                blob = bytes(row.blob)
                if blob[:1] == bytes([want]):
                    report.already[table] = report.already.get(table, 0) + 1
                    continue
                aad = dek_aad(row.id)
                new = box.encrypt(box.decrypt(blob, aad), aad)
                await conn.execute(
                    text(
                        f"UPDATE {table} SET {col} = :b, {ver} = :v"  # noqa: S608
                        f" WHERE {id_col} = CAST(:id AS uuid)"
                    ),
                    {"b": new, "v": box.kek_version, "id": row.id},
                )
                report.moved[table] = report.moved.get(table, 0) + 1
        users = (
            await conn.execute(
                text(
                    "SELECT id::text AS id, totp_secret_enc AS blob FROM app_user"
                    " WHERE totp_secret_enc IS NOT NULL FOR UPDATE"
                )
            )
        ).all()
        for row in users:
            blob = bytes(row.blob)
            if blob[:1] == bytes([want]):
                report.already["app_user.totp"] = report.already.get("app_user.totp", 0) + 1
                continue
            aad = totp_aad(row.id)
            await conn.execute(
                text("UPDATE app_user SET totp_secret_enc = :b WHERE id = CAST(:id AS uuid)"),
                {"b": box.encrypt(box.decrypt(blob, aad), aad), "id": row.id},
            )
            report.moved["app_user.totp"] = report.moved.get("app_user.totp", 0) + 1
        await audit.append(
            conn,
            action="secrets.migrated",
            actor_name=actor,
            target_type="vault",
            target_id=target,
            payload={"moved": report.moved, "already": report.already},
        )
    return report
