"""Retention of recorded SSH sessions and Operate chats, in two steps.

After X days an item moves to the archive (it leaves the everyday lists and stays searchable); after
Y days it is deleted, recording file included. X and Y are days, counted from when the item ended
(a chat: its last message). An item marked "keep" is archived like the others but never deleted by
this job. The job runs hourly and writes one audit event when it did something.
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from janus_ops import audit
from janus_ops.terminal.recorder import recording_path

log = logging.getLogger(__name__)
INTERVAL_S = 3600
BATCH = 500


@dataclass(frozen=True)
class Config:
    archive_after_days: int = 1
    delete_after_days: int = 30


@dataclass(frozen=True)
class Report:
    archived_recordings: int = 0
    archived_chats: int = 0
    deleted_recordings: int = 0
    deleted_chats: int = 0

    @property
    def total(self) -> int:
        return (
            self.archived_recordings
            + self.archived_chats
            + self.deleted_recordings
            + self.deleted_chats
        )


async def load(conn: AsyncConnection) -> Config:
    row = (
        await conn.execute(
            text("SELECT archive_after_days, delete_after_days FROM retention_config")
        )
    ).first()
    return Config(row.archive_after_days, row.delete_after_days) if row else Config()


def delete_recording_files(directory: Path | None, ids: list[str]) -> int:
    """Removes the .cast files of these sessions; the path is built from the id only."""
    if directory is None:
        return 0
    removed = 0
    for session_id in ids:
        path = recording_path(directory, session_id)
        if path is not None:
            try:
                path.unlink()
                removed += 1
            except OSError:
                log.warning("recording %s could not be removed", session_id)
    return removed


async def run(engine: AsyncEngine, recordings_dir: Path | None) -> Report:
    async with engine.begin() as conn:
        cfg = await load(conn)
        params = {"x": cfg.archive_after_days, "y": cfg.delete_after_days}
        rec_archived = (
            await conn.execute(
                text(
                    "UPDATE terminal_session SET archived_at = now() WHERE archived_at IS NULL"
                    " AND ended_at IS NOT NULL"
                    " AND ended_at < now() - make_interval(days => :x)"
                ),
                params,
            )
        ).rowcount
        chats_archived = (
            await conn.execute(
                text(
                    "UPDATE ai_operate_session s SET archived = true, archived_at = now()"
                    " WHERE s.archived_at IS NULL"
                    " AND s.updated_at < now() - make_interval(days => :x)"
                    " AND NOT EXISTS (SELECT 1 FROM ai_operate_message m WHERE m.session_id = s.id"
                    " AND m.status = 'pending')"
                ),
                params,
            )
        ).rowcount
        old = [
            str(r.id)
            for r in await conn.execute(
                text(
                    "SELECT id FROM terminal_session WHERE NOT keep AND ended_at IS NOT NULL"
                    " AND ended_at < now() - make_interval(days => :y) LIMIT :n"
                ),
                {**params, "n": BATCH},
            )
        ]
        if old:
            await conn.execute(
                text("DELETE FROM terminal_session WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": old},
            )
        chats_deleted = (
            await conn.execute(
                text(
                    "DELETE FROM ai_operate_session s WHERE NOT s.keep"
                    " AND s.updated_at < now() - make_interval(days => :y)"
                    " AND NOT EXISTS (SELECT 1 FROM ai_operate_message m WHERE m.session_id = s.id"
                    " AND m.status = 'pending')"
                ),
                params,
            )
        ).rowcount
        report = Report(rec_archived, chats_archived, len(old), chats_deleted)
        if report.total:
            await audit.append(
                conn,
                action="retention.run",
                actor_name="system",
                target_type="retention",
                target_id="sessions",
                payload={
                    "archive_after_days": cfg.archive_after_days,
                    "delete_after_days": cfg.delete_after_days,
                    "archived_recordings": report.archived_recordings,
                    "archived_chats": report.archived_chats,
                    "deleted_recordings": report.deleted_recordings,
                    "deleted_chats": report.deleted_chats,
                },
            )
    # files go after the rows: a failed unlink leaves an orphan file, never a row without its file
    delete_recording_files(recordings_dir, old)
    return report


async def loop(engine: AsyncEngine, recordings_dir: Path | None) -> None:
    await asyncio.sleep(30)  # let the server finish starting
    while True:
        try:
            report = await run(engine, recordings_dir)
            if report.total:
                log.info("retention: %s", report)
        except Exception:
            log.exception("retention run failed")
        await asyncio.sleep(INTERVAL_S)
