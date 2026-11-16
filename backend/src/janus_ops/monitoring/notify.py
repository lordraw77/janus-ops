"""Notifications (FR-34): channel senders and the dispatcher.

The dispatcher is a reconciler: every few seconds it compares open (and recently closed)
problems with `notification_delivery` and sends what is missing. A restart therefore neither
loses nor repeats notifications, and escalation is just "this rule matches after N seconds".

Not notified: acknowledged problems, problems in a downtime (FR-31, AC-08), unreachable checks
(parent down, FR-32, AC-07), flapping checks (FR-33). A problem still open when its downtime ends
is notified then. Recoveries go to the rules that were notified of the problem.

Senders use the standard library only (urllib, smtplib): HTTPS_PROXY / NO_PROXY of the server
process are honoured, nothing to install for air-gapped sites.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from janus_ops import __version__
from janus_ops.vault import Vault

log = logging.getLogger(__name__)

TICK_S = 10
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(seconds=60)
RECOVERY_WINDOW = timedelta(days=1)
HTTP_TIMEOUT_S = 15
RANK = {"warn": 1, "unknown": 1, "crit": 2}
MIN_RANK = {"warn": 1, "unknown": 1, "crit": 2}
ICON = {"crit": "🔴", "warn": "🟠", "unknown": "🟣", "ok": "🟢"}


class SendError(Exception):
    """Delivery failed; the message is stored (no secrets in it)."""


@dataclass(frozen=True)
class Message:
    event: str  # problem | recovery | test
    state: str  # ok | warn | crit | unknown
    node: str
    check: str
    output: str
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    problem_id: str | None = None
    environment: str | None = None

    @property
    def title(self) -> str:
        label = "RECOVERY" if self.event == "recovery" else self.state.upper()
        if self.event == "test":
            label = "TEST"
        if self.event == "workflow":
            label = "WORKFLOW"
        return f"[{label}] {self.node} / {self.check}"

    @property
    def body(self) -> str:
        lines = [self.output]
        if self.event == "recovery" and self.opened_at and self.closed_at:
            lines.append(f"Duration: {_duration(self.closed_at - self.opened_at)}")
        elif self.opened_at:
            lines.append(f"Since: {self.opened_at:%Y-%m-%d %H:%M:%S} UTC")
        return "\n".join(lines)

    def payload(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "state": self.state,
            "node": self.node,
            "check": self.check,
            "environment": self.environment,
            "output": self.output,
            "problem_id": self.problem_id,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


def _duration(delta: timedelta) -> str:
    s = int(delta.total_seconds())
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m {sec}s"


# ------------------------------------------------------------------ senders


def _post(url: str, body: bytes, headers: dict[str, str]) -> None:
    # http(s) only: channel URLs are validated when saved
    # Discord (Cloudflare) refuses urllib's default User-Agent with 403
    headers = {"User-Agent": f"Janus-Ops/{__version__}", **headers}
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
            resp.read(1 << 16)
    except urllib.error.HTTPError as exc:
        raise SendError(f"HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SendError(f"connection failed: {str(reason)[:200]}") from None


def _json_post(url: str, data: dict[str, Any], headers: dict[str, str] | None = None) -> None:
    _post(url, json.dumps(data).encode(), {"Content-Type": "application/json", **(headers or {})})


def send_telegram(config: dict[str, Any], secret: dict[str, Any], m: Message) -> None:
    text_ = f"{ICON.get(m.state, '')} <b>{_html(m.title)}</b>\n{_html(m.body)}"
    url = f"https://api.telegram.org/bot{secret['token']}/sendMessage"
    _json_post(url, {"chat_id": config["chat_id"], "text": text_, "parse_mode": "HTML"})


def send_webhook(config: dict[str, Any], secret: dict[str, Any], m: Message) -> None:
    body = json.dumps(m.payload()).encode()
    headers = {"Content-Type": "application/json"}
    if secret.get("hmac_key"):
        sig = hmac.new(secret["hmac_key"].encode(), body, hashlib.sha256).hexdigest()
        headers["X-Janus-Signature"] = f"sha256={sig}"
    _post(secret["url"], body, headers)


def send_discord(config: dict[str, Any], secret: dict[str, Any], m: Message) -> None:
    colors = {"crit": 0xEF4444, "warn": 0xF59E0B, "unknown": 0x8B5CF6, "ok": 0x10B981}
    data: dict[str, Any] = {
        "embeds": [
            {
                "title": m.title[:256],
                "description": m.body[:4000],
                "color": colors.get("ok" if m.event == "recovery" else m.state, 0x64748B),
            }
        ]
    }
    if config.get("username"):
        data["username"] = config["username"]
    _json_post(secret["url"], data)


def send_slack(config: dict[str, Any], secret: dict[str, Any], m: Message) -> None:
    _json_post(secret["url"], {"text": f"{ICON.get(m.state, '')} *{m.title}*\n{m.body}"})


def send_ntfy(config: dict[str, Any], secret: dict[str, Any], m: Message) -> None:
    server = config.get("url", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{urllib.parse.quote(config['topic'], safe='')}"
    priority = {"crit": "5", "warn": "4", "unknown": "3", "ok": "3"}
    priority.update({k: str(v) for k, v in (config.get("priorities") or {}).items()})
    level = "ok" if m.event == "recovery" else m.state
    headers = {
        # ntfy reads non-ASCII titles from RFC 2047 encoded headers; keep it ASCII
        "Title": m.title.encode("ascii", "replace").decode(),
        "Priority": priority.get(level, "3"),
        "Tags": {"crit": "red_circle", "warn": "warning", "ok": "green_circle"}.get(level, "bell"),
    }
    if secret.get("token"):
        headers["Authorization"] = f"Bearer {secret['token']}"
    _post(url, m.body.encode(), headers)


def send_email(config: dict[str, Any], secret: dict[str, Any], m: Message) -> None:
    msg = EmailMessage()
    msg["Subject"] = m.title
    msg["From"] = config["sender"]
    msg["To"] = ", ".join(config["recipients"])
    msg.set_content(m.body + "\n\n-- \nJanus-Ops")
    host, port = config["host"], int(config.get("port", 25))
    security = config.get("security", "starttls")
    ctx = ssl.create_default_context()
    if not config.get("verify_tls", True):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        smtp: smtplib.SMTP
        if security == "tls":
            smtp = smtplib.SMTP_SSL(host, port, timeout=HTTP_TIMEOUT_S, context=ctx)
        else:
            smtp = smtplib.SMTP(host, port, timeout=HTTP_TIMEOUT_S)
        with smtp:
            if security == "starttls":
                smtp.starttls(context=ctx)
            # authentication only when a user is configured: relays often accept without
            if config.get("username"):
                smtp.login(config["username"], secret.get("password", ""))
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise SendError("SMTP authentication failed") from None
    except (smtplib.SMTPException, OSError) as exc:
        raise SendError(f"SMTP: {type(exc).__name__}: {str(exc)[:200]}") from None


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


SENDERS = {
    "telegram": send_telegram,
    "webhook": send_webhook,
    "discord": send_discord,
    "slack": send_slack,
    "ntfy": send_ntfy,
    "email": send_email,
}


def channel_secret_id(channel_id: str) -> str:
    """Vault AAD namespace for channel secrets (never valid for a credential row)."""
    return f"channel-{channel_id}"


async def deliver(vault: Vault, channel: Any, message: Message, channel_id: str) -> None:
    secret: dict[str, Any] = {}
    if channel.secret_ciphertext is not None:
        secret = vault.reveal(
            channel_secret_id(channel_id),
            channel.secret_ciphertext,
            channel.secret_nonce,
            channel.secret_dek,
        )
    sender = SENDERS[channel.kind]
    await asyncio.to_thread(sender, channel.config, secret, message)


# ------------------------------------------------------------------ dispatcher

PROBLEMS = """
SELECT p.id, p.state, p.opened_at, p.closed_at, p.acknowledged_at,
       c.id AS check_id, c.name AS check_name, c.kind AS check_kind,
       n.id AS node_id, n.name AS node_name, n.environment,
       (coalesce(s.unreachable, false) OR EXISTS (
          -- parent down right now, even if this check has not been re-run since (FR-32)
          SELECT 1 FROM service_check hc JOIN check_state hs ON hs.check_id = hc.id
          WHERE hc.node_id = n.parent_id AND hc.kind = 'host' AND hc.enabled
            AND hs.hard_state = 'crit')) AS unreachable,
       coalesce(s.is_flapping, false) AS flapping,
       s.last_output,
       EXISTS (
         SELECT 1 FROM downtime d
         WHERE d.starts_at <= now() AND d.ends_at > now()
           AND (d.check_id = c.id OR d.node_id = n.id
                OR d.group_id IN (SELECT group_id FROM node_group_member m WHERE m.node_id = n.id))
       ) AS in_downtime
FROM problem p
JOIN service_check c ON c.id = p.check_id
JOIN node n ON n.id = c.node_id
LEFT JOIN check_state s ON s.check_id = c.id
WHERE p.closed_at IS NULL OR p.closed_at > now() - CAST(:window AS interval)
"""

RULES = """
SELECT r.id, r.name, r.min_state, r.filter, r.escalate_after_s,
       ch.id AS channel_id, ch.name AS channel_name, ch.kind, ch.config,
       ch.secret_ciphertext, ch.secret_nonce, ch.secret_dek
FROM notification_rule r JOIN notification_channel ch ON ch.id = r.channel_id
WHERE r.enabled AND ch.enabled
ORDER BY r.position, r.name
"""

DELIVERIES = """
SELECT problem_id, rule_id, event, state, status, max(sent_at) AS last, count(*) AS n
FROM notification_delivery WHERE problem_id = ANY(:ids)
GROUP BY problem_id, rule_id, event, state, status
"""


def rule_matches(rule: Any, problem: Any) -> bool:
    if RANK.get(problem.state, 0) < MIN_RANK[rule.min_state]:
        return False
    f: dict[str, Any] = rule.filter or {}
    if f.get("environments") and problem.environment not in f["environments"]:
        return False
    if f.get("node_ids") and str(problem.node_id) not in f["node_ids"]:
        return False
    return not (f.get("check_kinds") and problem.check_kind not in f["check_kinds"])


class Dispatcher:
    def __init__(self, engine: AsyncEngine, vault: Vault) -> None:
        self.db = engine
        self.vault = vault
        self.wake = asyncio.Event()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("notification dispatcher tick failed")
            self.wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), TICK_S)

    async def tick(self) -> int:
        now = datetime.now(UTC)
        async with self.db.connect() as conn:
            problems = list(await conn.execute(text(PROBLEMS), {"window": RECOVERY_WINDOW}))
            if not problems:
                return 0
            rules = list(await conn.execute(text(RULES)))
            if not rules:
                return 0
            done: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
            for d in await conn.execute(text(DELIVERIES), {"ids": [p.id for p in problems]}):
                key = (str(d.problem_id), str(d.rule_id), d.event, d.state)
                slot = done.setdefault(key, {"sent": False, "failed": 0, "last": None})
                if d.status == "sent":
                    slot["sent"] = True
                else:
                    slot["failed"] += d.n
                    slot["last"] = d.last
        jobs: list[tuple[Any, Any, str, str]] = []
        for p in problems:
            for r in rules:
                if p.closed_at is None:
                    quiet = p.acknowledged_at or p.in_downtime or p.unreachable or p.flapping
                    if quiet or not rule_matches(r, p):
                        continue
                    if r.escalate_after_s and now - p.opened_at < timedelta(
                        seconds=r.escalate_after_s
                    ):
                        continue
                    event, state = "problem", p.state
                else:
                    # recovery only for rules that told someone about the problem
                    if not any(
                        k[0] == str(p.id) and k[1] == str(r.id) and k[2] == "problem" and v["sent"]
                        for k, v in done.items()
                    ):
                        continue
                    event, state = "recovery", "ok"
                prior = done.get((str(p.id), str(r.id), event, state))
                if prior and (
                    prior["sent"]
                    or prior["failed"] >= MAX_ATTEMPTS
                    or (prior["last"] and now - prior["last"] < RETRY_AFTER)
                ):
                    continue
                jobs.append((p, r, event, state))
        for p, r, event, state in jobs:
            await self._send(p, r, event, state)
        return len(jobs)

    async def _send(self, p: Any, r: Any, event: str, state: str) -> None:
        message = Message(
            event=event,
            state=state if event == "problem" else p.state,
            node=p.node_name,
            check=p.check_name,
            output=p.last_output or "",
            opened_at=p.opened_at,
            closed_at=p.closed_at,
            problem_id=str(p.id),
            environment=p.environment,
        )
        status, error = "sent", None
        try:
            await deliver(self.vault, r, message, str(r.channel_id))
        except SendError as exc:
            status, error = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - e.g. vault key missing: record, keep going
            status, error = "failed", type(exc).__name__
        if status == "failed":
            log.warning(
                "notification failed: %s",
                error,
                extra={"channel": r.channel_name, "rule": r.name, "error": error},
            )
        async with self.db.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO notification_delivery"
                    " (problem_id, rule_id, channel_id, status, error, event, state)"
                    " VALUES (:p, :r, :c, :status, :err, :event, :state)"
                ),
                {
                    "p": p.id,
                    "r": r.id,
                    "c": r.channel_id,
                    "status": status,
                    "err": error,
                    "event": event,
                    "state": state,
                },
            )
