"""FRITZ!Box monitoring over TR-064, the box's SOAP API, in the Checkmk style.

The server polls the box with a user of its own (digest authentication, TR-064 must be enabled on
the box). One poll reads the device, the WAN link and the connection; each check item grades one
part of that reading, and discovery proposes the items the box offers. The credentials are read
from the vault, as for SNMP, and are never logged or returned.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Literal
from xml.sax.saxutils import escape

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from janus_ops.crypto import DecryptionError
from janus_ops.monitoring.checks import Result
from janus_ops.vault import Vault, VaultUnavailable

ITEMS = ("wan_link", "wan_connection", "uptime", "traffic")

# service -> (control URL, service type), from the box's tr64desc.xml
SERVICES: dict[str, tuple[str, str]] = {
    "deviceinfo": ("/upnp/control/deviceinfo", "urn:dslforum-org:service:DeviceInfo:1"),
    "wancommon": (
        "/upnp/control/wancommonifconfig1",
        "urn:dslforum-org:service:WANCommonInterfaceConfig:1",
    ),
    "wanip": ("/upnp/control/wanipconnection1", "urn:dslforum-org:service:WANIPConnection:1"),
    "wanppp": ("/upnp/control/wanpppconn1", "urn:dslforum-org:service:WANPPPConnection:1"),
}


class FritzError(Exception):
    """A stable code, shown as the check output; the message never carries the credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    user: str
    password: str
    timeout_s: float = 10.0


def envelope(service_type: str, action: str, args: dict[str, Any] | None = None) -> str:
    inner = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in (args or {}).items())
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service_type}">{inner}</u:{action}>'
        "</s:Body></s:Envelope>"
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_response(xml_text: str, action: str) -> dict[str, str]:
    """The out-arguments of `<action>Response`, as {name: text}. A SOAP fault raises FritzError."""
    # the box is ours, on the LAN; expat does not resolve external entities (see module notes)
    root = ET.fromstring(xml_text)  # noqa: S314
    for el in root.iter():
        name = _local(el.tag)
        if name == "errorCode":
            raise FritzError(f"soap:{(el.text or '').strip() or 'unknown'}")
        if name == f"{action}Response":
            return {_local(child.tag): (child.text or "").strip() for child in el}
    raise FritzError("soap:no_response")


async def _call(client: httpx2.AsyncClient, base: str, service: str, action: str) -> dict[str, str]:
    path, service_type = SERVICES[service]
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{service_type}#{action}"',
    }
    try:
        r = await client.post(base + path, content=envelope(service_type, action), headers=headers)
    except httpx2.HTTPError:
        raise FritzError("unreachable") from None
    if r.status_code == 401:
        raise FritzError("auth_failed")
    if r.status_code not in (200, 500):
        raise FritzError(f"http_{r.status_code}")
    try:
        return parse_response(r.text, action)
    except ET.ParseError:
        raise FritzError("bad_response") from None


async def poll(
    target: Target, transport: httpx2.AsyncBaseTransport | None = None
) -> dict[str, str]:
    """One reading of the box. The keys are the TR-064 out-arguments, with the connection status
    from the IP connection or, on a PPP connection, from the PPP one."""
    base = f"http://{target.host}:{target.port}"
    async with httpx2.AsyncClient(
        auth=httpx2.DigestAuth(target.user, target.password),
        timeout=target.timeout_s,
        transport=transport,
    ) as client:
        data: dict[str, str] = {}
        data.update(await _call(client, base, "deviceinfo", "GetInfo"))
        data.update(await _call(client, base, "wancommon", "GetCommonLinkProperties"))
        data.update(await _call(client, base, "wancommon", "GetTotalBytesReceived"))
        data.update(await _call(client, base, "wancommon", "GetTotalBytesSent"))
        # the IP connection, or the PPP one when the box runs PPPoE
        try:
            service = "wanip"
            data.update(await _call(client, base, service, "GetStatusInfo"))
        except FritzError as exc:
            if not exc.code.startswith("soap:"):
                raise
            service = "wanppp"
            data.update(await _call(client, base, service, "GetStatusInfo"))
        data.update(await _call(client, base, service, "GetExternalIPAddress"))
        return data


# ---- grading ------------------------------------------------------------------------------


def _mbit(raw: str | None) -> str:
    try:
        return f"{int(raw or 0) // 1_000_000} Mbit/s"
    except ValueError:
        return "? Mbit/s"


def _gib(raw: str | None) -> str:
    try:
        return f"{int(raw or 0) / 1024**3:.1f} GiB"
    except ValueError:
        return "? GiB"


def grade(item: str, data: dict[str, str]) -> Result:
    if item == "wan_link":
        link = data.get("NewPhysicalLinkStatus", "")
        down = _mbit(data.get("NewLayer1DownstreamMaxBitRate"))
        up = _mbit(data.get("NewLayer1UpstreamMaxBitRate"))
        access = data.get("NewWANAccessType", "?")
        out = f"WAN link {link or 'unknown'} ({access}, down {down}, up {up})"
        if link == "Up":
            return Result("ok", out)
        return Result("crit" if link == "Down" else "warn", out)
    if item == "wan_connection":
        status = data.get("NewConnectionStatus", "")
        out = (
            f"connection {status or 'unknown'}, external IP {data.get('NewExternalIPAddress', '?')}"
        )
        if status == "Connected":
            return Result("ok", out)
        if status in ("Connecting", "PendingDisconnect", "Disconnecting"):
            return Result("warn", out)
        return Result("crit", out)
    if item == "uptime":
        seconds = int(data.get("NewUpTime", "0") or 0)
        return Result(
            "ok",
            f"up {seconds // 86400} d {seconds % 86400 // 3600} h "
            f"({data.get('NewModelName', 'FRITZ!Box')}, "
            f"FritzOS {data.get('NewSoftwareVersion', '?')})",
        )
    if item == "traffic":
        return Result(
            "ok",
            f"received {_gib(data.get('NewTotalBytesReceived'))}, "
            f"sent {_gib(data.get('NewTotalBytesSent'))} since the last reboot",
        )
    raise ValueError(f"unknown item {item}")


def discover(data: dict[str, str]) -> list[str]:
    """The items this box offers: the same set for every FRITZ!Box with a WAN connection."""
    return list(ITEMS)


# ---- vault -----------------------------------------------------------------------------------

_LOAD = """
SELECT c.config, n.address, cr.id AS cred_id, cr.kind AS cred_kind, cr.username,
       cr.ciphertext, cr.nonce, cr.dek_wrapped
FROM node_capability c
JOIN node n ON n.id = c.node_id
LEFT JOIN credential cr ON cr.id = c.credential_id
WHERE c.node_id = CAST(:node AS uuid) AND c.kind = 'fritzbox' AND c.enabled
"""


async def load_target(conn: AsyncConnection, vault: Vault | None, node_id: str) -> Target:
    row = (await conn.execute(text(_LOAD), {"node": node_id})).first()
    if row is None:
        raise FritzError("no_fritzbox_capability")
    if row.cred_id is None or row.cred_kind != "fritzbox_password":
        raise FritzError("no_credential")
    if vault is None or not vault.available:
        raise FritzError("vault_unavailable")
    try:
        secret = vault.reveal(str(row.cred_id), row.ciphertext, row.nonce, row.dek_wrapped)
    except (VaultUnavailable, DecryptionError):
        raise FritzError("vault_unavailable") from None
    config: dict[str, Any] = row.config or {}
    return Target(
        host=row.address,
        port=int(config.get("port", 49000)),
        user=row.username or "",
        password=str(secret["secret"]),
    )


# ---- check -------------------------------------------------------------------------------------

_ERRORS: dict[str, tuple[Literal["unknown"], str]] = {
    "unreachable": ("unknown", "the FRITZ!Box does not answer (TR-064 port)"),
    "auth_failed": (
        "unknown",
        "the FRITZ!Box refused the user: check the credential and TR-064 access",
    ),
    "no_fritzbox_capability": ("unknown", "the node has no FRITZ!Box capability"),
    "no_credential": ("unknown", "the FRITZ!Box capability has no credential"),
    "vault_unavailable": ("unknown", "the vault is not available"),
}


def error_result(code: str) -> Result:
    if code in _ERRORS:
        state, msg = _ERRORS[code]
        return Result(state, msg)
    if code.startswith("soap:"):
        return Result("unknown", f"the FRITZ!Box answered with an error ({code[5:]})")
    return Result("unknown", f"FRITZ!Box: {code}")
