"""Move the master key between backends (FR-86, ADR-011): `janus-ops secrets migrate --to ...`.

Backends: `local` (JANUS_SECRET_KEY), `openbao-transit` (the key in Transit) and `openbao-kv`
(the content of each secret in the KV engine; the database keeps a reference).
- Between the two envelope backends only the small data keys (DEKs) are re-wrapped: the secrets
  stay as they are in the database, so the move is quick.
- To or from KV the content itself moves: it is read through the vault that wrote it and sealed
  again by the target vault. A secret that left KV is removed from KV after the database commit.
- Idempotent (rows already on the target are left alone), reversible with the same command, one
  database transaction: either every row moves or none does. TOTP seeds are always encrypted
  with the Transit or local key (never KV) and are moved with the envelope backends.
- Every move is written to the audit log (counts only).
`prune` removes secrets in KV that no row refers to any more (a deleted credential, a move that
could not finish).
"""

import logging
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops import audit
from janus_ops.crypto import Box
from janus_ops.transit import KvStore
from janus_ops.vault import KV_REF, SecretStore, Vault

log = logging.getLogger(__name__)

# table, id column, content column, nonce column, data key column, version column, id prefix.
# The vault id of a secret is its row id, with a prefix where the owner namespaces it: a channel's
# secret is sealed under "channel-<id>" (monitoring.notify.channel_secret_id).
TABLES = (
    ("credential", "id", "ciphertext", "nonce", "dek_wrapped", "kek_version", ""),
    (
        "notification_channel",
        "id",
        "secret_ciphertext",
        "secret_nonce",
        "secret_dek",
        "kek_version",
        "channel-",
    ),
    (
        "gitops_stack",
        "id",
        "secret_ciphertext",
        "secret_nonce",
        "secret_dek",
        "secret_kek_version",
        "",
    ),
    (
        "workflow",
        "id",
        "secret_ciphertext",
        "secret_nonce",
        "secret_dek",
        "secret_kek_version",
        "",
    ),
)
ENVELOPE_PREFIX = {"local": 1, "openbao-transit": 0x54}
BACKENDS = ("local", "openbao-transit", "openbao-kv")


@dataclass
class Report:
    moved: dict[str, int] = field(default_factory=dict)
    already: dict[str, int] = field(default_factory=dict)
    left_in_kv: list[str] = field(default_factory=list)  # ids whose KV copy could not be removed

    @property
    def total_moved(self) -> int:
        return sum(self.moved.values())


def dek_aad(entity_id: str) -> bytes:
    """Same binding as Vault.seal: the wrapped data key belongs to its row."""
    return f"credential:{entity_id}:dek".encode()


def totp_aad(user_id: str) -> bytes:
    return f"totp:{user_id}".encode()


def backend_of(content: bytes, dek: bytes) -> str:
    """Which backend wrote a stored secret."""
    if content.startswith(KV_REF):
        return "openbao-kv"
    if dek[:1] == bytes([ENVELOPE_PREFIX["local"]]):
        return "local"
    if dek[:1] == bytes([ENVELOPE_PREFIX["openbao-transit"]]):
        return "openbao-transit"
    return "unknown"


async def status(engine: AsyncEngine) -> dict[str, dict[str, int]]:
    """How many secrets each table has per backend."""
    out: dict[str, dict[str, int]] = {}
    async with engine.connect() as conn:
        for table, _id, ct, _nonce, dek, _ver, _prefix in TABLES:
            counts: dict[str, int] = {}
            rows = await conn.execute(
                text(f"SELECT {ct} AS ct, {dek} AS dek FROM {table} WHERE {ct} IS NOT NULL")  # noqa: S608
            )
            for r in rows:
                name = backend_of(bytes(r.ct), bytes(r.dek or b""))
                counts[name] = counts.get(name, 0) + 1
            out[table] = counts
        counts = {}
        for r in await conn.execute(
            text("SELECT totp_secret_enc AS e FROM app_user WHERE totp_secret_enc IS NOT NULL")
        ):
            first = bytes(r.e)[:1]
            name = next((n for n, b in ENVELOPE_PREFIX.items() if first == bytes([b])), "unknown")
            counts[name] = counts.get(name, 0) + 1
        out["app_user.totp"] = counts
    return out


async def migrate(
    engine: AsyncEngine,
    box: Box,
    target: str,
    store: SecretStore | None = None,
    actor: str = "cli",
) -> Report:
    """Move every secret that is not yet on `target`.

    `box` writes envelope secrets (its primary is the target when the target is an envelope
    backend) and reads old ones; `store` is the KV store, needed to read or write KV secrets."""
    if target not in BACKENDS:
        raise ValueError("unknown backend")
    if target == "openbao-kv" and store is None:
        raise ValueError("the KV store is not configured")
    reader = Vault(box, store)
    writer = Vault(box, store if target == "openbao-kv" else None)
    report = Report()
    freed: list[str] = []  # secrets that left KV: removed from KV once the move is committed
    async with engine.begin() as conn:
        for table, id_col, ct_col, nonce_col, dek_col, ver_col, prefix in TABLES:
            rows = (
                await conn.execute(
                    text(
                        f"SELECT {id_col}::text AS id, {ct_col} AS ct, {nonce_col} AS nonce,"  # noqa: S608
                        f" {dek_col} AS dek FROM {table} WHERE {ct_col} IS NOT NULL FOR UPDATE"
                    )
                )
            ).all()
            for row in rows:
                ct, nonce, dek = bytes(row.ct), bytes(row.nonce or b""), bytes(row.dek or b"")
                vault_id = prefix + row.id
                current = backend_of(ct, dek)
                if current == target:
                    report.already[table] = report.already.get(table, 0) + 1
                    continue
                if current in ENVELOPE_PREFIX and target in ENVELOPE_PREFIX:
                    aad = dek_aad(vault_id)  # same secret, its data key wrapped by another key
                    new_ct, new_nonce, new_dek = ct, nonce, box.encrypt(box.decrypt(dek, aad), aad)
                    version = box.kek_version
                else:
                    payload = reader.reveal(vault_id, ct, nonce, dek)
                    sealed = writer.seal(vault_id, payload)
                    new_ct, new_nonce, new_dek = sealed.ciphertext, sealed.nonce, sealed.dek_wrapped
                    version = sealed.kek_version
                    if current == "openbao-kv":
                        freed.append(vault_id)
                await conn.execute(
                    text(
                        f"UPDATE {table} SET {ct_col} = :c, {nonce_col} = :n, {dek_col} = :d,"  # noqa: S608
                        f" {ver_col} = :v WHERE {id_col} = CAST(:id AS uuid)"
                    ),
                    {"c": new_ct, "n": new_nonce, "d": new_dek, "v": version, "id": row.id},
                )
                report.moved[table] = report.moved.get(table, 0) + 1
        if target in ENVELOPE_PREFIX:
            await _move_totp(conn, box, ENVELOPE_PREFIX[target], report)
        await audit.append(
            conn,
            action="secrets.migrated",
            actor_name=actor,
            target_type="vault",
            target_id=target,
            payload={"moved": report.moved, "already": report.already},
        )
    for entity_id in freed:
        try:
            if store is not None:
                store.delete(entity_id)
        except Exception:  # noqa: BLE001 - the move is committed; a leftover is for `prune`
            report.left_in_kv.append(entity_id)
            log.warning("a secret left KV but its copy could not be removed: run secrets prune")
    return report


async def _move_totp(conn: AsyncConnection, box: Box, want: int, report: Report) -> None:
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


async def referenced_ids(engine: AsyncEngine) -> set[str]:
    """Ids of every secret the database keeps in KV."""
    ids: set[str] = set()
    async with engine.connect() as conn:
        for table, id_col, ct_col, _nonce, _dek, _ver, prefix in TABLES:
            query = (
                f"SELECT {id_col}::text AS id FROM {table}"  # noqa: S608
                f" WHERE substring({ct_col} FROM 1 FOR 4) = 'kv1:'::bytea"
            )
            rows = await conn.execute(text(query))
            ids.update(prefix + r.id for r in rows)
    return ids


async def orphans(engine: AsyncEngine, store: KvStore) -> set[str]:
    """Secrets in KV that no row refers to."""
    return store.entities() - await referenced_ids(engine)
