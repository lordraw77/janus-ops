"""A thin async client for the Proxmox VE REST API (/api2/json).

ADR C4: Proxmoxer is synchronous, so this module talks to the API with httpx2's AsyncClient.
Two ways to authenticate, both stored as vault credentials:

- API token (`proxmox_token`, user@realm!tokenid + secret): an Authorization header per request.
- user + password (`proxmox_password`): a ticket from POST /access/ticket, valid for two hours;
  it is renewed after TICKET_TTL_S and once more when the API answers 401. Unsafe requests add
  the CSRFPreventionToken that comes with the ticket.
"""

import asyncio
import ssl
import time
from dataclasses import dataclass, field
from typing import Any

import httpx2

TICKET_TTL_S = 90 * 60
TIMEOUT_S = 20.0


class PveError(Exception):
    """`code` is shown to the user (api.ts messages); `detail` is the API's own message."""

    def __init__(self, code: str, status: int = 0, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code, self.status, self.detail = code, status, detail


@dataclass(frozen=True)
class Auth:
    kind: str  # proxmox_token | proxmox_password
    username: str  # root@pam or root@pam!janus
    secret: str = field(repr=False)


def ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:  # self-signed PVE certificates; set per node in the capability
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _detail(response: httpx2.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, dict) and errors:
            return "; ".join(f"{k}: {v}".strip() for k, v in errors.items())[:300]
        if body.get("message"):
            return str(body["message"]).strip()[:300]
    reason = response.reason_phrase or ""
    return reason.strip()[:300] or None


class PveClient:
    def __init__(
        self,
        host: str,
        port: int,
        verify: bool,
        auth: Auth,
        transport: httpx2.AsyncBaseTransport | None = None,  # tests
    ) -> None:
        self.host, self.port, self.verify, self.auth = host, port, verify, auth
        self.base = f"https://{host}:{port}/api2/json"
        self._http = httpx2.AsyncClient(
            verify=ssl_context(verify), timeout=TIMEOUT_S, transport=transport
        )
        self._ticket: tuple[str, str, float] | None = None  # ticket, csrf, obtained at
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    async def _login(self) -> tuple[str, str]:
        async with self._lock:
            if self._ticket and time.monotonic() - self._ticket[2] < TICKET_TTL_S:
                return self._ticket[0], self._ticket[1]
            try:
                r = await self._http.post(
                    f"{self.base}/access/ticket",
                    data={"username": self.auth.username, "password": self.auth.secret},
                )
            except httpx2.HTTPError:
                raise PveError("pve_unreachable") from None
            if r.status_code == 401:
                raise PveError("pve_auth_failed", 401)
            if r.status_code >= 400:
                raise PveError("pve_error", r.status_code, _detail(r))
            data = r.json().get("data") or {}
            if data.get("NeedTFA"):
                raise PveError("pve_tfa_required", 401)
            self._ticket = (data["ticket"], data["CSRFPreventionToken"], time.monotonic())
            return data["ticket"], data["CSRFPreventionToken"]

    async def headers(self, unsafe: bool) -> dict[str, str]:
        """Authentication headers; also used for the console WebSocket."""
        if self.auth.kind == "proxmox_token":
            return {"Authorization": f"PVEAPIToken={self.auth.username}={self.auth.secret}"}
        ticket, csrf = await self._login()
        out = {"Cookie": f"PVEAuthCookie={ticket}"}
        if unsafe:
            out["CSRFPreventionToken"] = csrf
        return out

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """`data` of the reply; PveError on failure. One retry with a new ticket on 401."""
        for attempt in (0, 1):
            headers = await self.headers(method != "GET")
            try:
                r = await self._http.request(
                    method,
                    f"{self.base}{path}",
                    params=_clean(params),
                    data=_clean(data) if method != "GET" else None,
                    headers=headers,
                )
            except httpx2.TimeoutException:
                raise PveError("pve_timeout") from None
            except httpx2.HTTPError:
                raise PveError("pve_unreachable") from None
            if r.status_code == 401 and self.auth.kind == "proxmox_password" and attempt == 0:
                self._ticket = None
                continue
            if r.status_code == 401:
                raise PveError("pve_auth_failed", 401)
            if r.status_code == 403:
                raise PveError("pve_forbidden", 403, _detail(r))
            if r.status_code >= 400:
                raise PveError("pve_error", r.status_code, _detail(r))
            return r.json().get("data")
        raise PveError("pve_auth_failed", 401)  # pragma: no cover - loop always returns

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", path, data=data)

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("DELETE", path, params=params)


def _clean(values: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop None; booleans as 0/1 as the PVE API expects."""
    if values is None:
        return None
    return {k: int(v) if isinstance(v, bool) else v for k, v in values.items() if v is not None}
