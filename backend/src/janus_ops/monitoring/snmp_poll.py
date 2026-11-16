"""SNMP reads for NAS, switches and UPS (F8, FR-82): the server polls a node's SNMP agent.

Read-only: GET of one numeric OID. There is no SET, no walk and no trap receiver. The node's
`snmp` capability gives the port, the version and the SNMPv3 security level; its credential gives
the community (v2c, kind `generic`) or the SNMPv3 user (kind `snmp_v3`: the user name, the
authentication passphrase as `secret` and the privacy passphrase as `passphrase`). Secrets come
from the vault and are never logged or returned.

Algorithms: SNMPv3 accepts SHA and SHA-256 for authentication and AES-128 or AES-256 for privacy.
MD5 and DES are not offered (both are broken); v1 is not offered.

A fresh SnmpEngine is used for each read: pysnmp binds an engine to the event loop that first
uses it, and the check engine and the tests run in different loops.
"""

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Any

from pyasn1.type import univ
from pysnmp.hlapi.v3arch.asyncio import (
    USM_AUTH_HMAC96_SHA,
    USM_AUTH_HMAC192_SHA256,
    USM_PRIV_CFB128_AES,
    USM_PRIV_CFB256_AES,
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    UdpTransportTarget,
    UsmUserData,
    get_cmd,
    walk_cmd,
)
from pysnmp.proto import rfc1902, rfc1905
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.crypto import DecryptionError
from janus_ops.vault import Vault, VaultUnavailable

log = logging.getLogger(__name__)

MAX_TEXT = 200
NO_SUCH = (rfc1905.NoSuchObject, rfc1905.NoSuchInstance, rfc1905.EndOfMibView)
AUTH = {"SHA": USM_AUTH_HMAC96_SHA, "SHA256": USM_AUTH_HMAC192_SHA256}
PRIV = {"AES128": USM_PRIV_CFB128_AES, "AES256": USM_PRIV_CFB256_AES}


class SnmpError(Exception):
    """Configuration problem found before any packet is sent; `code` is stable and shown."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    version: str  # "v2c" or "v3"
    community: str | None = None
    user: str | None = None
    security_level: str = "authPriv"  # noAuthNoPriv | authNoPriv | authPriv
    auth_protocol: str = "SHA256"
    priv_protocol: str = "AES128"
    auth_secret: str | None = None
    priv_secret: str | None = None


@dataclass(frozen=True)
class Reading:
    """One GET: the value as text, the number when it is numeric, or the reason it failed."""

    text: str
    number: float | None
    error: str | None = None


_LOAD = """
SELECT c.config, n.address, cr.id AS cred_id, cr.kind AS cred_kind, cr.username,
       cr.ciphertext, cr.nonce, cr.dek_wrapped
FROM node_capability c
JOIN node n ON n.id = c.node_id
LEFT JOIN credential cr ON cr.id = c.credential_id
WHERE c.node_id = CAST(:node AS uuid) AND c.kind = 'snmp' AND c.enabled
"""


async def load_target(conn: AsyncConnection, vault: Vault | None, node_id: str) -> Target:
    """The poll target of a node, with its secrets revealed. Raises SnmpError with a stable code."""
    row = (await conn.execute(text(_LOAD), {"node": node_id})).first()
    if row is None:
        raise SnmpError("no_snmp_capability")
    if row.cred_id is None:
        raise SnmpError("no_credential")
    config: dict[str, Any] = row.config or {}
    version = str(config.get("version", "v2c"))
    expected = "generic" if version == "v2c" else "snmp_v3"
    if row.cred_kind != expected:
        raise SnmpError("credential_kind_mismatch")
    if vault is None or not vault.available:
        raise SnmpError("vault_unavailable")
    try:
        secret = vault.reveal(str(row.cred_id), row.ciphertext, row.nonce, row.dek_wrapped)
    except (VaultUnavailable, DecryptionError):
        raise SnmpError("vault_unavailable") from None
    port = int(config.get("port", 161))
    if version == "v2c":
        return Target(host=row.address, port=port, version="v2c", community=str(secret["secret"]))
    return Target(
        host=row.address,
        port=port,
        version="v3",
        user=row.username,
        security_level=str(config.get("security_level", "authPriv")),
        auth_protocol=str(config.get("auth_protocol", "SHA256")),
        priv_protocol=str(config.get("priv_protocol", "AES128")),
        auth_secret=str(secret["secret"]),
        priv_secret=str(secret.get("passphrase", "")) or None,
    )


def _auth(target: Target) -> CommunityData | UsmUserData:
    if target.version == "v2c":
        if not target.community:
            raise SnmpError("no_community")
        # v2c is an explicit choice of the operator (the capability default is v2c only for
        # devices that have no v3); it sends the community in clear text
        return CommunityData(target.community, mpModel=1)  # noqa: S508 - mpModel 1 = SNMPv2c
    if not target.user:
        raise SnmpError("no_user")
    level = target.security_level
    if level == "noAuthNoPriv":
        # explicit operator choice (read-only OIDs on a trusted segment); not the default
        return UsmUserData(target.user)  # noqa: S509 - noAuthNoPriv chosen by the operator
    if not target.auth_secret:
        raise SnmpError("no_auth_passphrase")
    if level == "authNoPriv":
        # authentication without encryption: chosen by the operator, the default is authPriv
        return UsmUserData(
            target.user, authKey=target.auth_secret, authProtocol=AUTH[target.auth_protocol]
        )
    if level != "authPriv":
        raise SnmpError("bad_security_level")
    if not target.priv_secret:
        raise SnmpError("no_priv_passphrase")
    return UsmUserData(
        target.user,
        authKey=target.auth_secret,
        privKey=target.priv_secret,
        authProtocol=AUTH[target.auth_protocol],
        privProtocol=PRIV[target.priv_protocol],
    )


def _opaque_number(raw: bytes) -> float | None:
    """A number inside an Opaque (RFC 3965): float (tag 9f78, length 4) or double (9f79, length 8).
    Synology UPS values use it; anything else is not a number we can read."""
    if len(raw) == 7 and raw[:3] == b"\x9f\x78\x04":
        return float(struct.unpack(">f", raw[3:])[0])
    if len(raw) == 11 and raw[:3] == b"\x9f\x79\x08":
        return float(struct.unpack(">d", raw[3:])[0])
    return None


def classify(value: Any) -> Reading:
    """The text of a varbind value, and its number when the type is numeric."""
    if isinstance(value, NO_SUCH):
        return Reading(type(value).__name__, None, error="no_such_object")
    if isinstance(value, rfc1902.Opaque):
        number = _opaque_number(bytes(value.asOctets()))
        if number is not None:
            return Reading(f"{number:g}", number)
    if isinstance(value, univ.Integer):
        number = float(int(value))
        return Reading(str(int(value)), number)
    text_value = value.prettyPrint()
    return Reading(text_value[:MAX_TEXT], None)


async def get(target: Target, oid: str, timeout_s: float) -> Reading:
    """One SNMP GET. Network failures and timeouts come back as a Reading with an error code."""
    try:
        auth = _auth(target)
    except SnmpError as exc:
        return Reading("", None, error=exc.code)
    engine = SnmpEngine()
    try:
        transport = await UdpTransportTarget.create(
            (target.host, target.port), timeout=timeout_s, retries=0
        )
        indication, status, _index, binds = await get_cmd(
            engine,
            auth,
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
            lookupMib=False,
        )
    except Exception as exc:  # noqa: BLE001 - pysnmp raises many types; all are reported as one code
        log.warning("snmp get failed: %s", type(exc).__name__)
        return Reading("", None, error="request_failed")
    finally:
        engine.close_dispatcher()
    if indication:
        # the indication text names the timeout or the authentication failure; no secret is in it
        return Reading("", None, error=_indication_code(str(indication)))
    if status:
        return Reading("", None, error="pdu_error")
    if not binds:
        return Reading("", None, error="no_such_object")
    return classify(binds[0][1])


def _indication_code(text_value: str) -> str:
    lowered = text_value.lower()
    if "timeout" in lowered:
        return "timeout"
    if "digest" in lowered or "unknownusername" in lowered or "notinwindow" in lowered:
        return "authentication_failed"
    if "unknowncontext" in lowered or "unknownengine" in lowered:
        return "engine_unknown"
    return "request_failed"


MAX_WALK_ROWS = 500
WALK_BUDGET_S = 60.0


@dataclass(frozen=True)
class Row:
    """One OID returned by a walk: its text and value, and its number when numeric."""

    oid: str
    text: str
    number: float | None


@dataclass(frozen=True)
class Walk:
    rows: list[Row]
    truncated: bool
    error: str | None


async def walk(target: Target, root: str, *, max_rows: int = MAX_WALK_ROWS) -> Walk:
    """Every OID under `root`, read with GETNEXT. Read-only; stops at the end of the subtree,
    at `max_rows`, or when the device stops answering (the reason comes back as `error`)."""
    try:
        auth = _auth(target)
    except SnmpError as exc:
        return Walk([], False, exc.code)
    engine = SnmpEngine()
    rows: list[Row] = []
    truncated = False
    error: str | None = None
    try:
        transport = await UdpTransportTarget.create(
            (target.host, target.port), timeout=5, retries=0
        )
        async with asyncio.timeout(WALK_BUDGET_S):
            async for indication, status, _index, binds in walk_cmd(
                engine,
                auth,
                transport,
                ContextData(),
                ObjectType(ObjectIdentity(root)),
                lookupMib=False,
                # stop at the end of the subtree; the default walks the rest of the MIB
                lexicographicMode=False,
            ):
                if indication:
                    error = _indication_code(str(indication))
                    break
                if status:
                    error = "pdu_error"
                    break
                done = False
                for bind in binds:
                    oid = bind[0].prettyPrint()
                    if not oid.startswith(root + "."):
                        done = True  # past the subtree: the walk is over
                        break
                    if len(rows) >= max_rows:
                        truncated = True
                        done = True
                        break
                    reading = classify(bind[1])
                    if reading.error == "no_such_object":
                        continue
                    rows.append(Row(oid, reading.text, reading.number))
                if done:
                    break
    except TimeoutError:
        error = "walk_timeout"
    except Exception as exc:  # noqa: BLE001 - pysnmp raises many types; reported as one code
        log.warning("snmp walk failed: %s", type(exc).__name__)
        error = "request_failed"
    finally:
        engine.close_dispatcher()
    return Walk(rows, truncated, error)
