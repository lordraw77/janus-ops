"""Export and import of the configuration (F8).

One JSON document, `janus-ops-config`, with a section per kind of thing: environments,
credentials, nodes (tags, capabilities, services), groups, check templates, checks, SNMP OIDs,
notifications, snippets, workflows and AI providers (models, routes).

- **Names, not ids.** Every internal id in the document (a credential on a capability, the node in
  a check, the channel in a workflow step) is written as a token `@node:web01`, `@channel:Mail`,
  `@check:web01/CPU`, `@credential:ssh-key`, `@group:lab`, and resolved by name on import, so a
  document moves between installations.
- **Secrets are never in the clear.** Credentials, notification channel secrets and AI keys
  travel in one block encrypted with a passphrase (scrypt + AES-256-GCM). Without a passphrase
  the document has no secrets and credentials are listed by name only: an import then needs
  those credentials to exist already.
- **Import** reads names from the document and matches them in the database. `skip` leaves what
  exists untouched; `overwrite` replaces it. Everything happens in one transaction, each item in a
  savepoint: one bad item is reported and the rest goes on. A dry run does the same and rolls back
  (nothing is sealed in the vault either).

Not included, on purpose: users and roles (they come from the directory), the audit log, metrics and
history, agents (their identity belongs to the host), GitOps stacks, Ansible sources, downtimes and
discovery.
"""

import base64
import json
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops import __version__
from janus_ops.crypto import DecryptionError
from janus_ops.vault import Sealed, Vault, VaultUnavailable

FORMAT = "janus-ops-config"
VERSION = 1
SECTIONS = (
    "environments",
    "credentials",
    "nodes",
    "groups",
    "templates",
    "checks",
    "snmp_oids",
    "notifications",
    "snippets",
    "workflows",
    "ai_providers",
)
# a section needs these others to exist (in the document or already in the database)
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
MIN_PASSPHRASE = 12
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ConfigError(Exception):
    """A document or a request that cannot be used; `code` is stable for the API."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ the encrypted secrets block
def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode())


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def encrypt_secrets(passphrase: str, data: dict[str, Any]) -> dict[str, Any]:
    if len(passphrase) < MIN_PASSPHRASE:
        raise ConfigError("passphrase_too_short")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = _key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    blob = AESGCM(key).encrypt(nonce, json.dumps(data).encode(), FORMAT.encode())
    return {
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "ciphertext": _b64(blob),
    }


def decrypt_secrets(passphrase: str, block: dict[str, Any]) -> dict[str, Any]:
    try:
        if block.get("kdf") != "scrypt":
            raise ConfigError("unsupported_secrets")
        n, r, p = int(block["n"]), int(block["r"]), int(block["p"])
        if n > 2**20 or r > 32 or p > 16:  # a document must not choose an unbounded cost
            raise ConfigError("unsupported_secrets")
        key = _key(passphrase, base64.b64decode(block["salt"]), n, r, p)
        plain = AESGCM(key).decrypt(
            base64.b64decode(block["nonce"]), base64.b64decode(block["ciphertext"]), FORMAT.encode()
        )
    except InvalidTag:
        raise ConfigError("wrong_passphrase") from None
    except (KeyError, ValueError, TypeError):
        raise ConfigError("unsupported_secrets") from None
    data: dict[str, Any] = json.loads(plain)
    return data


# ------------------------------------------------------------------ ids <-> names
@dataclass
class Refs:
    """The names of the rows an id in the document may point to."""

    node: dict[str, str] = field(default_factory=dict)
    channel: dict[str, str] = field(default_factory=dict)
    check: dict[str, str] = field(default_factory=dict)
    credential: dict[str, str] = field(default_factory=dict)
    group: dict[str, str] = field(default_factory=dict)

    @classmethod
    async def load(cls, conn: AsyncConnection) -> "Refs":
        async def pairs(sql: str) -> dict[str, str]:
            return {str(r[0]): str(r[1]) for r in await conn.execute(text(sql))}

        return cls(
            node=await pairs("SELECT id, name FROM node"),
            channel=await pairs("SELECT id, name FROM notification_channel"),
            check=await pairs(
                "SELECT c.id, n.name || '/' || c.name FROM service_check c"
                " JOIN node n ON n.id = c.node_id"
            ),
            credential=await pairs("SELECT id, name FROM credential"),
            group=await pairs("SELECT id, name FROM node_group"),
        )

    def tokenize(self, value: Any) -> Any:
        """The value with every known id replaced by `@kind:name`."""
        if isinstance(value, dict):
            return {k: self.tokenize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.tokenize(v) for v in value]
        if isinstance(value, str) and UUID_RE.match(value):
            for kind in ("node", "channel", "check", "credential", "group"):
                name = getattr(self, kind).get(value.lower())
                if name is not None:
                    return f"@{kind}:{name}"
        return value


TOKEN_RE = re.compile(r"^@(node|channel|check|credential|group):(.+)$", re.S)


class Resolver:
    """`@kind:name` back to ids, from the database as it is during the import."""

    def __init__(self) -> None:
        self.maps: dict[str, dict[str, str]] = {}
        self.missing: set[str] = set()

    async def refresh(self, conn: AsyncConnection, *kinds: str) -> None:
        sql = {
            "node": "SELECT lower(name), id FROM node",
            "channel": "SELECT lower(name), id FROM notification_channel",
            "check": "SELECT lower(n.name || '/' || c.name), c.id FROM service_check c"
            " JOIN node n ON n.id = c.node_id",
            "credential": "SELECT lower(name), id FROM credential",
            "group": "SELECT lower(name), id FROM node_group",
        }
        for kind in kinds or tuple(sql):
            self.maps[kind] = {str(r[0]): str(r[1]) for r in await conn.execute(text(sql[kind]))}

    def lookup(self, kind: str, name: str) -> str | None:
        return self.maps.get(kind, {}).get(name.lower())

    def resolve(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if isinstance(value, str):
            m = TOKEN_RE.match(value)
            if m:
                found = self.lookup(m.group(1), m.group(2))
                if found is None:
                    self.missing.add(f"{m.group(1)} {m.group(2)}")
                return found
        return value


def _j(value: Any) -> str:
    return json.dumps(value, default=str)


async def _rows(conn: AsyncConnection, sql: str, **params: Any) -> list[Any]:
    return list((await conn.execute(text(sql), params)).all())


# ------------------------------------------------------------------ export
@dataclass
class Exported:
    sections: dict[str, Any]
    secrets: dict[str, Any]
    warnings: list[str]


async def export_config(
    conn: AsyncConnection, vault: Vault, sections: list[str], include_secrets: bool
) -> Exported:
    wanted = [s for s in SECTIONS if s in sections]
    refs = await Refs.load(conn)
    out: dict[str, Any] = {}
    sec: dict[str, Any] = {"credentials": {}, "channels": {}, "ai_keys": {}}
    warn: list[str] = []

    if "environments" in wanted:
        out["environments"] = [
            {"name": r.name, "label": r.label, "color": r.color, "position": r.position}
            for r in await _rows(
                conn, "SELECT name, label, color, position FROM environment ORDER BY position, name"
            )
        ]

    if "credentials" in wanted:
        items = []
        for r in await _rows(
            conn,
            "SELECT id, name, kind, username, description, public_info, ciphertext, nonce,"
            " dek_wrapped FROM credential ORDER BY name",
        ):
            items.append(
                {
                    "name": r.name,
                    "kind": r.kind,
                    "username": r.username,
                    "description": r.description,
                    "public_info": r.public_info or {},
                }
            )
            if include_secrets:
                try:
                    sec["credentials"][r.name] = vault.reveal(
                        str(r.id), r.ciphertext, r.nonce, r.dek_wrapped
                    )
                except (VaultUnavailable, DecryptionError):
                    warn.append(f"credential {r.name}: the secret could not be read")
        out["credentials"] = items

    if "nodes" in wanted:
        nodes = []
        for n in await _rows(
            conn,
            "SELECT n.id, n.name, n.address, n.environment, n.description, p.name AS parent"
            " FROM node n LEFT JOIN node p ON p.id = n.parent_id ORDER BY n.name",
        ):
            tags = await _rows(
                conn,
                "SELECT t.name FROM node_tag nt JOIN tag t ON t.id = nt.tag_id"
                " WHERE nt.node_id = :n ORDER BY t.name",
                n=n.id,
            )
            caps = await _rows(
                conn,
                "SELECT c.kind, c.enabled, c.config, cr.name AS credential FROM node_capability c"
                " LEFT JOIN credential cr ON cr.id = c.credential_id WHERE c.node_id = :n"
                " ORDER BY c.kind",
                n=n.id,
            )
            svcs = await _rows(
                conn,
                "SELECT s.kind, s.name, s.enabled, s.config, cr.name AS credential"
                " FROM node_service s LEFT JOIN credential cr ON cr.id = s.credential_id"
                " WHERE s.node_id = :n ORDER BY s.name",
                n=n.id,
            )
            nodes.append(
                {
                    "name": n.name,
                    "address": n.address,
                    "environment": n.environment,
                    "description": n.description,
                    "parent": n.parent,
                    "tags": [t.name for t in tags],
                    "capabilities": [
                        {
                            "kind": c.kind,
                            "enabled": c.enabled,
                            "config": refs.tokenize(c.config),
                            "credential": c.credential,
                        }
                        for c in caps
                    ],
                    "services": [
                        {
                            "kind": s.kind,
                            "name": s.name,
                            "enabled": s.enabled,
                            "config": refs.tokenize(s.config),
                            "credential": s.credential,
                        }
                        for s in svcs
                    ],
                }
            )
        out["nodes"] = nodes

    if "groups" in wanted:
        groups = []
        for g in await _rows(
            conn, "SELECT id, name, description, ansible_vars FROM node_group ORDER BY name"
        ):
            members = await _rows(
                conn,
                "SELECT n.name FROM node_group_member m JOIN node n ON n.id = m.node_id"
                " WHERE m.group_id = :g ORDER BY n.name",
                g=g.id,
            )
            groups.append(
                {
                    "name": g.name,
                    "description": g.description,
                    "ansible_vars": g.ansible_vars,
                    "members": [m.name for m in members],
                }
            )
        out["groups"] = groups

    if "templates" in wanted:
        # the templates shipped with the product come back with the product
        out["templates"] = [
            {
                "name": t.name,
                "category": t.category,
                "description": t.description,
                "requires": t.requires,
                "macros": t.macros,
                "items": t.items,
                "detect": t.detect,
            }
            for t in await _rows(
                conn,
                "SELECT name, category, description, requires, macros, items, detect"
                " FROM check_template WHERE source <> 'built-in' ORDER BY name",
            )
        ]

    if "checks" in wanted:
        out["checks"] = [
            {
                "node": c.node,
                "name": c.name,
                "kind": c.kind,
                "params": refs.tokenize(c.params),
                "comparison": c.comparison,
                "warn_threshold": c.warn_threshold,
                "crit_threshold": c.crit_threshold,
                "check_cycles": c.check_cycles,
                "interval_s": c.interval_s,
                "enabled": c.enabled,
                "source": c.source,
                "template": c.template,
                "template_item": c.template_item,
            }
            for c in await _rows(
                conn,
                "SELECT n.name AS node, c.name, c.kind, c.params, c.comparison, c.warn_threshold,"
                " c.crit_threshold, c.check_cycles, c.interval_s, c.enabled, c.source,"
                " t.name AS template, c.template_item FROM service_check c"
                " JOIN node n ON n.id = c.node_id"
                " LEFT JOIN check_template t ON t.id = c.template_id"
                " ORDER BY n.name, c.name",
            )
        ]

    if "snmp_oids" in wanted:
        out["snmp_oids"] = [
            {
                "name": o.name,
                "oid": o.oid,
                "unit": o.unit,
                "description": o.description,
                "indexed": o.indexed,
                "verified": o.verified,
                "vendor": o.vendor,
                "source": o.source,
                "device_type": o.device_type,
            }
            for o in await _rows(
                conn,
                "SELECT name, oid, unit, description, indexed, verified, vendor, source,"
                " device_type FROM snmp_oid ORDER BY name",
            )
        ]

    if "notifications" in wanted:
        channels = []
        for c in await _rows(
            conn,
            "SELECT id, name, kind, config, enabled, secret_ciphertext, secret_nonce, secret_dek"
            " FROM notification_channel ORDER BY name",
        ):
            channels.append(
                {"name": c.name, "kind": c.kind, "config": c.config, "enabled": c.enabled}
            )
            if include_secrets and c.secret_ciphertext is not None:
                try:
                    sec["channels"][c.name] = vault.reveal(
                        f"channel-{c.id}", c.secret_ciphertext, c.secret_nonce, c.secret_dek
                    )
                except (VaultUnavailable, DecryptionError):
                    warn.append(f"channel {c.name}: the secret could not be read")
        out["notifications"] = {
            "channels": channels,
            "rules": [
                {
                    "name": r.name,
                    "channel": r.channel,
                    "min_state": r.min_state,
                    "filter": refs.tokenize(r.filter),
                    "escalate_after_s": r.escalate_after_s,
                    "position": r.position,
                    "enabled": r.enabled,
                }
                for r in await _rows(
                    conn,
                    "SELECT r.name, c.name AS channel, r.min_state, r.filter, r.escalate_after_s,"
                    " r.position, r.enabled FROM notification_rule r"
                    " JOIN notification_channel c ON c.id = r.channel_id"
                    " ORDER BY r.position, r.name",
                )
            ],
        }

    if "snippets" in wanted:
        out["snippets"] = [
            {"name": s.name, "description": s.description, "body": s.body}
            for s in await _rows(
                conn,
                "SELECT name, description, body FROM snippet WHERE owner_id IS NULL ORDER BY name",
            )
        ]

    if "workflows" in wanted:
        out["workflows"] = [
            {
                "name": w.name,
                "description": w.description,
                "enabled": w.enabled,
                "definition": refs.tokenize(w.definition),
            }
            for w in await _rows(
                conn, "SELECT name, description, enabled, definition FROM workflow ORDER BY name"
            )
        ]

    if "ai_providers" in wanted:
        providers = []
        for p in await _rows(conn, "SELECT * FROM ai_provider ORDER BY slug"):
            models = await _rows(
                conn,
                "SELECT model_id, kind, context_window, tools, price_in, price_out,"
                " first_token_timeout, manual, available, test_ok FROM ai_provider_model"
                " WHERE provider_id = :p ORDER BY model_id",
                p=p.id,
            )
            keys = await _rows(
                conn,
                "SELECT id, label, last4, secret_ciphertext, secret_nonce, secret_dek"
                " FROM ai_provider_key WHERE provider_id = :p ORDER BY created_at, id",
                p=p.id,
            )
            key_meta = []
            for k in keys:
                key_meta.append({"label": k.label, "last4": k.last4})
                if include_secrets:
                    try:
                        value = vault.reveal(
                            str(k.id), k.secret_ciphertext, k.secret_nonce, k.secret_dek
                        )
                        sec["ai_keys"].setdefault(p.slug, []).append(value.get("api_key", ""))
                    except (VaultUnavailable, DecryptionError):
                        warn.append(f"AI provider {p.slug}: a key could not be read")
            providers.append(
                {
                    "slug": p.slug,
                    "type": p.type,
                    "base_url": p.base_url,
                    "headers": p.headers,
                    "extra_body": p.extra_body,
                    "config": p.config,
                    "connect_timeout": p.connect_timeout,
                    "first_token_timeout": p.first_token_timeout,
                    "total_timeout": p.total_timeout,
                    "verify_tls": p.verify_tls,
                    "enabled": p.enabled,
                    "budget_tokens_daily": p.budget_tokens_daily,
                    "budget_tokens_monthly": p.budget_tokens_monthly,
                    "keys": key_meta,
                    "models": [dict(m._mapping) for m in models],
                }
            )
        routes = []
        for r in await _rows(conn, "SELECT name FROM ai_route ORDER BY name"):
            entries = await _rows(
                conn,
                "SELECT e.position, p.slug, e.model_id, e.key_id FROM ai_route_entry e"
                " JOIN ai_provider p ON p.id = e.provider_id WHERE e.route = :r"
                " ORDER BY e.position",
                r=r.name,
            )
            routes.append(
                {
                    "name": r.name,
                    "entries": [{"provider": e.slug, "model": e.model_id} for e in entries],
                }
            )
        out["ai_providers"] = {"providers": providers, "routes": routes}

    return Exported(out, sec if include_secrets else {}, warn)


def build_document(exported: Exported, passphrase: str | None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "app_version": __version__,
        "sections": exported.sections,
    }
    if exported.secrets:
        if not passphrase:
            raise ConfigError("passphrase_required")
        doc["secrets"] = encrypt_secrets(passphrase, exported.secrets)
    if exported.warnings:
        doc["warnings"] = exported.warnings
    return doc


# ------------------------------------------------------------------ import
@dataclass
class SectionReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "notes": self.notes,
        }


def check_document(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ConfigError("not_a_config_file")
    if not isinstance(doc.get("version"), int) or doc["version"] > VERSION:
        raise ConfigError("unsupported_version")
    if not isinstance(doc.get("sections"), dict):
        raise ConfigError("not_a_config_file")
    return doc


class Importer:
    def __init__(
        self,
        conn: AsyncConnection,
        vault: Vault,
        doc: dict[str, Any],
        secrets_data: dict[str, Any],
        overwrite: bool,
        dry_run: bool,
        actor: str,
    ) -> None:
        self.conn, self.vault, self.doc = conn, vault, doc["sections"]
        self.secrets, self.overwrite, self.dry_run, self.actor = (
            secrets_data,
            overwrite,
            dry_run,
            actor,
        )
        self.res = Resolver()
        self.reports: dict[str, SectionReport] = {}

    def seal(self, entity_id: str, payload: dict[str, Any]) -> Sealed:
        if self.dry_run:  # nothing reaches the vault (or an external KV) in a preview
            return Sealed(b"dry-run", b"", b"", 1)
        return self.vault.seal(entity_id, payload)

    async def one(self, section: str, name: str, work: Any) -> None:
        """Run `work` (returns "created", "updated" or "skipped") in a savepoint."""
        rep = self.reports[section]
        try:
            async with self.conn.begin_nested():
                outcome = await work()
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the import
            reason = str(getattr(exc, "orig", exc)).splitlines()[0][:200]
            rep.failed.append({"name": name, "error": reason})
            return
        setattr(rep, outcome, getattr(rep, outcome) + 1)

    async def run(self, sections: list[str]) -> dict[str, Any]:
        order = [s for s in SECTIONS if s in sections and s in self.doc]
        for s in order:
            self.reports[s] = SectionReport()
        await self.res.refresh(self.conn)
        for s in order:
            await getattr(self, f"_{s}")(self.doc[s])
            if s in ("credentials", "nodes", "checks", "notifications", "groups"):
                await self.res.refresh(self.conn)
        for s in order:
            if self.res.missing and s in ("workflows",):
                self.reports[s].notes.append(
                    "not found, left empty: " + ", ".join(sorted(self.res.missing))
                )
        return {s: r.out() for s, r in self.reports.items()}

    # -- helpers
    async def _id(self, sql: str, **p: Any) -> str | None:
        v = await self.conn.scalar(text(sql), p)
        return str(v) if v is not None else None

    # -- sections
    async def _environments(self, items: list[dict[str, Any]]) -> None:
        for e in items:

            async def work(e: dict[str, Any] = e) -> str:
                exists = await self._id("SELECT name FROM environment WHERE name = :n", n=e["name"])
                if exists and not self.overwrite:
                    return "skipped"
                await self.conn.execute(
                    text(
                        "INSERT INTO environment (name, label, color, position)"
                        " VALUES (:n, :l, :c, :p) ON CONFLICT (name) DO UPDATE"
                        " SET label = :l, color = :c, position = :p"
                    ),
                    {
                        "n": e["name"],
                        "l": e["label"],
                        "c": e.get("color"),
                        "p": e.get("position", 0),
                    },
                )
                return "updated" if exists else "created"

            await self.one("environments", e.get("name", "?"), work)

    async def _credentials(self, items: list[dict[str, Any]]) -> None:
        rep = self.reports["credentials"]
        have = self.secrets.get("credentials", {})
        for c in items:

            async def work(c: dict[str, Any] = c) -> str:
                payload = have.get(c["name"])
                existing = await self._id("SELECT id FROM credential WHERE name = :n", n=c["name"])
                if existing and not self.overwrite:
                    return "skipped"
                if payload is None:
                    rep.notes.append(f"{c['name']}: no secret in the file")
                    return "skipped"
                new_id = existing or str(uuid.uuid4())
                sealed = self.seal(new_id, payload)
                params = {
                    "id": new_id,
                    "n": c["name"],
                    "k": c["kind"],
                    "u": c.get("username"),
                    "d": c.get("description"),
                    "pi": _j(c.get("public_info") or {}),
                    "ct": sealed.ciphertext,
                    "no": sealed.nonce,
                    "dek": sealed.dek_wrapped,
                    "kv": sealed.kek_version,
                }
                if existing:
                    await self.conn.execute(
                        text(
                            "UPDATE credential SET kind = :k, username = :u, description = :d,"
                            " public_info = CAST(:pi AS jsonb), ciphertext = :ct, nonce = :no,"
                            " dek_wrapped = :dek, kek_version = :kv, updated_at = now()"
                            " WHERE id = CAST(:id AS uuid)"
                        ),
                        params,
                    )
                    return "updated"
                await self.conn.execute(
                    text(
                        "INSERT INTO credential (id, name, kind, username, description,"
                        " public_info,"
                        " ciphertext, nonce, dek_wrapped, kek_version) VALUES (CAST(:id AS uuid),"
                        " :n, :k, :u, :d, CAST(:pi AS jsonb), :ct, :no, :dek, :kv)"
                    ),
                    params,
                )
                return "created"

            await self.one("credentials", c.get("name", "?"), work)
        if items and not have:
            rep.notes.append("the file has no secrets: credentials that do not exist are skipped")

    async def _nodes(self, items: list[dict[str, Any]]) -> None:
        rep = self.reports["nodes"]
        made: list[tuple[dict[str, Any], str]] = []
        # pass 1: the nodes themselves
        for n in items:

            async def work(n: dict[str, Any] = n) -> str:
                existing = await self._id(
                    "SELECT id FROM node WHERE lower(name) = lower(:n)", n=n["name"]
                )
                if existing and not self.overwrite:
                    return "skipped"
                p = {
                    "n": n["name"],
                    "a": n["address"],
                    "e": n.get("environment", "prod"),
                    "d": n.get("description"),
                }
                if existing:
                    await self.conn.execute(
                        text(
                            "UPDATE node SET address = :a, environment = :e, description = :d,"
                            " updated_at = now() WHERE id = CAST(:id AS uuid)"
                        ),
                        {**p, "id": existing},
                    )
                    node_id = existing
                else:
                    node_id = str(
                        await self.conn.scalar(
                            text(
                                "INSERT INTO node (name, address, environment, description)"
                                " VALUES (:n, :a, :e, :d) RETURNING id"
                            ),
                            p,
                        )
                    )
                made.append((n, node_id))
                return "updated" if existing else "created"

            await self.one("nodes", n.get("name", "?"), work)
        await self.res.refresh(self.conn, "node")
        # pass 2: what points at other nodes or at credentials
        for n, node_id in made:

            async def work2(n: dict[str, Any] = n, node_id: str = node_id) -> str:
                parent = self.res.lookup("node", n["parent"]) if n.get("parent") else None
                await self.conn.execute(
                    text(
                        "UPDATE node SET parent_id = CAST(:p AS uuid) WHERE id = CAST(:id AS uuid)"
                    ),
                    {"p": parent, "id": node_id},
                )
                await self.conn.execute(
                    text("DELETE FROM node_tag WHERE node_id = CAST(:id AS uuid)"), {"id": node_id}
                )
                for tag in n.get("tags", []):
                    tag_id = await self.conn.scalar(
                        text(
                            "INSERT INTO tag (name) VALUES (:t) ON CONFLICT (lower(name))"
                            " DO UPDATE SET name = tag.name RETURNING id"
                        ),
                        {"t": tag},
                    )
                    await self.conn.execute(
                        text(
                            "INSERT INTO node_tag (node_id, tag_id) VALUES (CAST(:n AS uuid), :t)"
                            " ON CONFLICT DO NOTHING"
                        ),
                        {"n": node_id, "t": tag_id},
                    )
                await self.conn.execute(
                    text("DELETE FROM node_capability WHERE node_id = CAST(:id AS uuid)"),
                    {"id": node_id},
                )
                for cap in n.get("capabilities", []):
                    cred = (
                        self.res.lookup("credential", cap["credential"])
                        if cap.get("credential")
                        else None
                    )
                    if cap.get("credential") and cred is None:
                        rep.notes.append(
                            f"{n['name']}: credential {cap['credential']} does not exist"
                        )
                    await self.conn.execute(
                        text(
                            "INSERT INTO node_capability (node_id, kind, enabled, config,"
                            " credential_id) VALUES (CAST(:n AS uuid), :k, :e, CAST(:c AS jsonb),"
                            " CAST(:cr AS uuid))"
                        ),
                        {
                            "n": node_id,
                            "k": cap["kind"],
                            "e": cap.get("enabled", True),
                            "c": _j(self.res.resolve(cap.get("config", {}))),
                            "cr": cred,
                        },
                    )
                await self.conn.execute(
                    text("DELETE FROM node_service WHERE node_id = CAST(:id AS uuid)"),
                    {"id": node_id},
                )
                for svc in n.get("services", []):
                    cred = (
                        self.res.lookup("credential", svc["credential"])
                        if svc.get("credential")
                        else None
                    )
                    await self.conn.execute(
                        text(
                            "INSERT INTO node_service (node_id, kind, name, enabled, config,"
                            " credential_id) VALUES (CAST(:n AS uuid), :k, :nm, :e,"
                            " CAST(:c AS jsonb), CAST(:cr AS uuid))"
                        ),
                        {
                            "n": node_id,
                            "k": svc["kind"],
                            "nm": svc["name"],
                            "e": svc.get("enabled", True),
                            "c": _j(self.res.resolve(svc.get("config", {}))),
                            "cr": cred,
                        },
                    )
                return "updated"

            # a failure here is reported on the node but does not undo its creation
            try:
                async with self.conn.begin_nested():
                    await work2()
            except Exception as exc:  # noqa: BLE001
                reason = str(getattr(exc, "orig", exc)).splitlines()[0][:200]
                rep.failed.append({"name": f"{n.get('name', '?')} (capabilities)", "error": reason})

    async def _groups(self, items: list[dict[str, Any]]) -> None:
        for g in items:

            async def work(g: dict[str, Any] = g) -> str:
                existing = await self._id(
                    "SELECT id FROM node_group WHERE lower(name) = lower(:n)", n=g["name"]
                )
                if existing and not self.overwrite:
                    return "skipped"
                if existing:
                    gid = existing
                    await self.conn.execute(
                        text(
                            "UPDATE node_group SET description = :d, ansible_vars ="
                            " CAST(:v AS jsonb) WHERE id = CAST(:id AS uuid)"
                        ),
                        {
                            "d": g.get("description"),
                            "v": _j(g.get("ansible_vars") or {}),
                            "id": gid,
                        },
                    )
                    await self.conn.execute(
                        text("DELETE FROM node_group_member WHERE group_id = CAST(:id AS uuid)"),
                        {"id": gid},
                    )
                else:
                    gid = str(
                        await self.conn.scalar(
                            text(
                                "INSERT INTO node_group (name, description, ansible_vars)"
                                " VALUES (:n, :d, CAST(:v AS jsonb)) RETURNING id"
                            ),
                            {
                                "n": g["name"],
                                "d": g.get("description"),
                                "v": _j(g.get("ansible_vars") or {}),
                            },
                        )
                    )
                for member in g.get("members", []):
                    node_id = self.res.lookup("node", member)
                    if node_id is None:
                        self.reports["groups"].notes.append(f"{g['name']}: node {member} not found")
                        continue
                    await self.conn.execute(
                        text(
                            "INSERT INTO node_group_member (group_id, node_id) VALUES"
                            " (CAST(:g AS uuid), CAST(:n AS uuid)) ON CONFLICT DO NOTHING"
                        ),
                        {"g": gid, "n": node_id},
                    )
                return "updated" if existing else "created"

            await self.one("groups", g.get("name", "?"), work)

    async def _templates(self, items: list[dict[str, Any]]) -> None:
        for t in items:

            async def work(t: dict[str, Any] = t) -> str:
                existing = await self._id(
                    "SELECT id FROM check_template WHERE lower(name) = lower(:n)", n=t["name"]
                )
                if existing and not self.overwrite:
                    return "skipped"
                p = {
                    "n": t["name"],
                    "c": t["category"],
                    "d": t.get("description", ""),
                    "r": t.get("requires"),
                    "m": _j(t.get("macros", [])),
                    "i": _j(t["items"]),
                    "de": _j(t.get("detect", {})),
                    "by": self.actor,
                }
                if existing:
                    await self.conn.execute(
                        text(
                            "UPDATE check_template SET category = :c, description = :d,"
                            " requires = :r,"
                            " macros = CAST(:m AS jsonb), items = CAST(:i AS jsonb),"
                            " detect = CAST(:de AS jsonb), version = version + 1,"
                            " updated_at = now()"
                            " WHERE id = CAST(:id AS uuid) AND source <> 'built-in'"
                        ),
                        {**p, "id": existing},
                    )
                    return "updated"
                await self.conn.execute(
                    text(
                        "INSERT INTO check_template (name, category, description, requires, macros,"
                        " items, detect, source, created_by) VALUES (:n, :c, :d, :r,"
                        " CAST(:m AS jsonb), CAST(:i AS jsonb), CAST(:de AS jsonb), 'manual', :by)"
                    ),
                    p,
                )
                return "created"

            await self.one("templates", t.get("name", "?"), work)

    async def _checks(self, items: list[dict[str, Any]]) -> None:
        rep = self.reports["checks"]
        for c in items:

            async def work(c: dict[str, Any] = c) -> str:
                node_id = self.res.lookup("node", c["node"])
                if node_id is None:
                    rep.notes.append(f"{c['node']}/{c['name']}: node not found")
                    return "skipped"
                existing = await self._id(
                    "SELECT id FROM service_check WHERE node_id = CAST(:n AS uuid) AND name = :nm",
                    n=node_id,
                    nm=c["name"],
                )
                if existing and not self.overwrite:
                    return "skipped"
                template_id = None
                if c.get("template"):
                    template_id = await self._id(
                        "SELECT id FROM check_template WHERE lower(name) = lower(:n)",
                        n=c["template"],
                    )
                p = {
                    "n": node_id,
                    "nm": c["name"],
                    "k": c["kind"],
                    "pa": _j(self.res.resolve(c.get("params", {}))),
                    "cmp": c.get("comparison"),
                    "w": c.get("warn_threshold"),
                    "cr": c.get("crit_threshold"),
                    "cy": c.get("check_cycles", 1),
                    "iv": c.get("interval_s", 60),
                    "en": c.get("enabled", True),
                    "so": c.get("source", "manual"),
                    "t": template_id,
                    "ti": c.get("template_item") if template_id else None,
                }
                if existing:
                    await self.conn.execute(
                        text(
                            "UPDATE service_check SET kind = :k, params = CAST(:pa AS jsonb),"
                            " comparison = :cmp, warn_threshold = :w, crit_threshold = :cr,"
                            " check_cycles = :cy, interval_s = :iv, enabled = :en, source = :so,"
                            " template_id = CAST(:t AS uuid), template_item = :ti,"
                            " updated_at = now()"
                            " WHERE id = CAST(:id AS uuid)"
                        ),
                        {**p, "id": existing},
                    )
                    return "updated"
                await self.conn.execute(
                    text(
                        "INSERT INTO service_check (node_id, name, kind, params, comparison,"
                        " warn_threshold, crit_threshold, check_cycles, interval_s, enabled,"
                        " source,"
                        " template_id, template_item) VALUES (CAST(:n AS uuid), :nm, :k,"
                        " CAST(:pa AS jsonb), :cmp, :w, :cr, :cy, :iv, :en, :so, CAST(:t AS uuid),"
                        " :ti)"
                    ),
                    p,
                )
                return "created"

            await self.one("checks", f"{c.get('node', '?')}/{c.get('name', '?')}", work)

    async def _snmp_oids(self, items: list[dict[str, Any]]) -> None:
        for o in items:

            async def work(o: dict[str, Any] = o) -> str:
                existing = await self._id(
                    "SELECT id FROM snmp_oid WHERE lower(name) = lower(:n)", n=o["name"]
                )
                if existing and not self.overwrite:
                    return "skipped"
                p = {
                    "n": o["name"],
                    "o": o["oid"],
                    "u": o.get("unit", ""),
                    "d": o.get("description", ""),
                    "i": o.get("indexed", False),
                    "v": o.get("verified", False),
                    "ve": o.get("vendor", ""),
                    "s": o.get("source", "manual"),
                    "dt": o.get("device_type", "generic"),
                    "by": self.actor,
                }
                if existing:
                    await self.conn.execute(
                        text(
                            "UPDATE snmp_oid SET oid = :o, unit = :u, description = :d,"
                            " indexed = :i,"
                            " verified = :v, vendor = :ve, device_type = :dt"
                            " WHERE id = CAST(:id AS uuid)"
                        ),
                        {**p, "id": existing},
                    )
                    return "updated"
                await self.conn.execute(
                    text(
                        "INSERT INTO snmp_oid (name, oid, unit, description, indexed, verified,"
                        " vendor, source, created_by, device_type) VALUES (:n, :o, :u, :d, :i, :v,"
                        " :ve, :s, :by, :dt)"
                    ),
                    p,
                )
                return "created"

            await self.one("snmp_oids", o.get("name", "?"), work)

    async def _notifications(self, body: dict[str, Any]) -> None:
        have = self.secrets.get("channels", {})
        for c in body.get("channels", []):

            async def work(c: dict[str, Any] = c) -> str:
                existing = await self._id(
                    "SELECT id FROM notification_channel WHERE name = :n", n=c["name"]
                )
                if existing and not self.overwrite:
                    return "skipped"
                channel_id = existing or str(uuid.uuid4())
                payload = have.get(c["name"])
                sealed = (
                    self.seal(f"channel-{channel_id}", payload) if payload is not None else None
                )
                p = {
                    "id": channel_id,
                    "n": c["name"],
                    "k": c["kind"],
                    "c": _j(c.get("config", {})),
                    "e": c.get("enabled", True),
                    "ct": sealed.ciphertext if sealed else None,
                    "no": sealed.nonce if sealed else None,
                    "dek": sealed.dek_wrapped if sealed else None,
                    "kv": sealed.kek_version if sealed else None,
                }
                if existing:
                    secret_sql = (
                        ", secret_ciphertext = :ct, secret_nonce = :no, secret_dek = :dek,"
                        " kek_version = :kv"
                        if sealed
                        else ""
                    )
                    await self.conn.execute(
                        text(
                            "UPDATE notification_channel SET kind = :k,"  # noqa: S608
                            " config = CAST(:c AS jsonb),"
                            f" enabled = :e{secret_sql}, updated_at = now()"
                            " WHERE id = CAST(:id AS uuid)"
                        ),
                        p,
                    )
                    return "updated"
                await self.conn.execute(
                    text(
                        "INSERT INTO notification_channel (id, name, kind, config, enabled,"
                        " secret_ciphertext, secret_nonce, secret_dek, kek_version) VALUES"
                        " (CAST(:id AS uuid), :n, :k, CAST(:c AS jsonb), :e, :ct, :no, :dek, :kv)"
                    ),
                    p,
                )
                return "created"

            await self.one("notifications", f"channel {c.get('name', '?')}", work)
        await self.res.refresh(self.conn, "channel")
        for r in body.get("rules", []):

            async def work_rule(r: dict[str, Any] = r) -> str:
                channel_id = self.res.lookup("channel", r["channel"])
                if channel_id is None:
                    self.reports["notifications"].notes.append(
                        f"rule {r['name']}: channel {r['channel']} not found"
                    )
                    return "skipped"
                existing = await self._id(
                    "SELECT id FROM notification_rule WHERE name = :n", n=r["name"]
                )
                if existing and not self.overwrite:
                    return "skipped"
                p = {
                    "n": r["name"],
                    "ch": channel_id,
                    "m": r.get("min_state", "crit"),
                    "f": _j(self.res.resolve(r.get("filter", {}))),
                    "es": r.get("escalate_after_s"),
                    "po": r.get("position", 0),
                    "en": r.get("enabled", True),
                }
                if existing:
                    await self.conn.execute(
                        text(
                            "UPDATE notification_rule SET channel_id = CAST(:ch AS uuid),"
                            " min_state = :m, filter = CAST(:f AS jsonb), escalate_after_s = :es,"
                            " position = :po, enabled = :en WHERE id = CAST(:id AS uuid)"
                        ),
                        {**p, "id": existing},
                    )
                    return "updated"
                await self.conn.execute(
                    text(
                        "INSERT INTO notification_rule (name, channel_id, min_state, filter,"
                        " escalate_after_s, position, enabled) VALUES (:n, CAST(:ch AS uuid), :m,"
                        " CAST(:f AS jsonb), :es, :po, :en)"
                    ),
                    p,
                )
                return "created"

            await self.one("notifications", f"rule {r.get('name', '?')}", work_rule)

    async def _snippets(self, items: list[dict[str, Any]]) -> None:
        for s in items:

            async def work(s: dict[str, Any] = s) -> str:
                existing = await self._id(
                    "SELECT id FROM snippet WHERE owner_id IS NULL AND lower(name) = lower(:n)",
                    n=s["name"],
                )
                if existing and not self.overwrite:
                    return "skipped"
                if existing:
                    await self.conn.execute(
                        text(
                            "UPDATE snippet SET description = :d, body = :b, updated_at = now()"
                            " WHERE id = CAST(:id AS uuid)"
                        ),
                        {"d": s.get("description"), "b": s["body"], "id": existing},
                    )
                    return "updated"
                await self.conn.execute(
                    text(
                        "INSERT INTO snippet (owner_id, name, description, body, created_by)"
                        " VALUES (NULL, :n, :d, :b, :by)"
                    ),
                    {"n": s["name"], "d": s.get("description"), "b": s["body"], "by": self.actor},
                )
                return "created"

            await self.one("snippets", s.get("name", "?"), work)

    async def _workflows(self, items: list[dict[str, Any]]) -> None:
        for w in items:

            async def work(w: dict[str, Any] = w) -> str:
                existing = await self._id(
                    "SELECT id FROM workflow WHERE lower(name) = lower(:n)", n=w["name"]
                )
                if existing and not self.overwrite:
                    return "skipped"
                p = {
                    "n": w["name"],
                    "d": w.get("description", ""),
                    "e": w.get("enabled", True),
                    "df": _j(self.res.resolve(w["definition"])),
                    "by": self.actor,
                }
                if existing:
                    await self.conn.execute(
                        text(
                            "UPDATE workflow SET description = :d, enabled = :e,"
                            " definition = CAST(:df AS jsonb), updated_at = now()"
                            " WHERE id = CAST(:id AS uuid)"
                        ),
                        {**p, "id": existing},
                    )
                    return "updated"
                await self.conn.execute(
                    text(
                        "INSERT INTO workflow (name, description, definition, enabled, created_by)"
                        " VALUES (:n, :d, CAST(:df AS jsonb), :e, :by)"
                    ),
                    p,
                )
                return "created"

            await self.one("workflows", w.get("name", "?"), work)

    async def _ai_providers(self, body: dict[str, Any]) -> None:
        have = self.secrets.get("ai_keys", {})
        for p in body.get("providers", []):

            async def work(p: dict[str, Any] = p) -> str:
                existing = await self._id("SELECT id FROM ai_provider WHERE slug = :s", s=p["slug"])
                if existing and not self.overwrite:
                    return "skipped"
                v = {
                    "s": p["slug"],
                    "t": p["type"],
                    "u": p["base_url"],
                    "h": _j(p.get("headers", {})),
                    "x": _j(p.get("extra_body", {})),
                    "c": _j(p.get("config", {})),
                    "ct": p.get("connect_timeout", 10),
                    "ft": p.get("first_token_timeout", 60),
                    "tt": p.get("total_timeout", 300),
                    "vt": p.get("verify_tls", "true"),
                    "e": p.get("enabled", True),
                    "bd": p.get("budget_tokens_daily"),
                    "bm": p.get("budget_tokens_monthly"),
                    "by": self.actor,
                }
                if existing:
                    pid = existing
                    await self.conn.execute(
                        text(
                            "UPDATE ai_provider SET type = :t, base_url = :u, headers ="
                            " CAST(:h AS jsonb), extra_body = CAST(:x AS jsonb), config ="
                            " CAST(:c AS jsonb), connect_timeout = :ct, first_token_timeout = :ft,"
                            " total_timeout = :tt, verify_tls = :vt, enabled = :e,"
                            " budget_tokens_daily = :bd, budget_tokens_monthly = :bm,"
                            " updated_at = now() WHERE id = CAST(:id AS uuid)"
                        ),
                        {**v, "id": pid},
                    )
                else:
                    pid = str(
                        await self.conn.scalar(
                            text(
                                "INSERT INTO ai_provider (slug, type, base_url, headers,"
                                " extra_body,"
                                " config, connect_timeout, first_token_timeout, total_timeout,"
                                " verify_tls, enabled, budget_tokens_daily, budget_tokens_monthly,"
                                " created_by) VALUES (:s, :t, :u, CAST(:h AS jsonb),"
                                " CAST(:x AS jsonb), CAST(:c AS jsonb), :ct, :ft, :tt, :vt, :e,"
                                " :bd, :bm, :by) RETURNING id"
                            ),
                            v,
                        )
                    )
                for m in p.get("models", []):
                    await self.conn.execute(
                        text(
                            "INSERT INTO ai_provider_model (provider_id, model_id, kind,"
                            " context_window, tools, price_in, price_out, first_token_timeout,"
                            " manual, available, test_ok) VALUES (CAST(:p AS uuid), :m, :k, :cw,"
                            " :t, :pi, :po, :ft, :ma, :av, :ok) ON CONFLICT (provider_id, model_id)"
                            " DO UPDATE SET kind = :k, context_window = :cw, tools = :t,"
                            " manual = :ma, available = :av, test_ok = :ok"
                        ),
                        {
                            "p": pid,
                            "m": m["model_id"],
                            "k": m.get("kind", "chat"),
                            "cw": m.get("context_window"),
                            "t": m.get("tools"),
                            "pi": m.get("price_in"),
                            "po": m.get("price_out"),
                            "ft": m.get("first_token_timeout"),
                            "ma": m.get("manual", False),
                            "av": m.get("available", True),
                            "ok": m.get("test_ok"),
                        },
                    )
                keys = have.get(p["slug"], [])
                if keys:
                    if existing:
                        await self.conn.execute(
                            text(
                                "DELETE FROM ai_provider_key WHERE provider_id = CAST(:p AS uuid)"
                            ),
                            {"p": pid},
                        )
                    for i, value in enumerate(keys):
                        key_id = str(uuid.uuid4())
                        sealed = self.seal(key_id, {"api_key": value})
                        meta = (
                            (p.get("keys") or [{}] * (i + 1))[i]
                            if i < len(p.get("keys", []))
                            else {}
                        )
                        await self.conn.execute(
                            text(
                                "INSERT INTO ai_provider_key (id, provider_id, label, last4,"
                                " secret_ciphertext, secret_nonce, secret_dek, secret_kek_version)"
                                " VALUES (CAST(:id AS uuid), CAST(:p AS uuid), :l, :l4, :ct, :no,"
                                " :dek, :kv)"
                            ),
                            {
                                "id": key_id,
                                "p": pid,
                                "l": meta.get("label", ""),
                                "l4": meta.get("last4", value[-4:]),
                                "ct": sealed.ciphertext,
                                "no": sealed.nonce,
                                "dek": sealed.dek_wrapped,
                                "kv": sealed.kek_version,
                            },
                        )
                elif p.get("keys"):
                    self.reports["ai_providers"].notes.append(
                        f"{p['slug']}: no keys in the file, add them in AI providers"
                    )
                return "updated" if existing else "created"

            await self.one("ai_providers", f"provider {p.get('slug', '?')}", work)
        for r in body.get("routes", []):

            async def work_route(r: dict[str, Any] = r) -> str:
                existing = await self._id("SELECT name FROM ai_route WHERE name = :n", n=r["name"])
                entries = await self._id(
                    "SELECT count(*) FROM ai_route_entry WHERE route = :n", n=r["name"]
                )
                if existing and int(entries or 0) > 0 and not self.overwrite:
                    return "skipped"
                if not existing:
                    await self.conn.execute(
                        text("INSERT INTO ai_route (name) VALUES (:n)"), {"n": r["name"]}
                    )
                await self.conn.execute(
                    text("DELETE FROM ai_route_entry WHERE route = :n"), {"n": r["name"]}
                )
                position = 0
                for e in r.get("entries", []):
                    pid = await self._id(
                        "SELECT id FROM ai_provider WHERE slug = :s", s=e["provider"]
                    )
                    if pid is None:
                        self.reports["ai_providers"].notes.append(
                            f"route {r['name']}: provider {e['provider']} not found"
                        )
                        continue
                    await self.conn.execute(
                        text(
                            "INSERT INTO ai_route_entry (route, position, provider_id, model_id)"
                            " VALUES (:r, :po, CAST(:p AS uuid), :m)"
                        ),
                        {"r": r["name"], "po": position, "p": pid, "m": e["model"]},
                    )
                    position += 1
                return "updated" if existing else "created"

            await self.one("ai_providers", f"route {r.get('name', '?')}", work_route)
