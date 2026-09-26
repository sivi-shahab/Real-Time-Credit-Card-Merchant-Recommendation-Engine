"""Maker-checker for the learning switches (ADR-0011, threat T-10).

Env vars give the values a deployment starts with. The first approved change stores the
full set in `learning_settings`, and from then on the stored values win over env, so a
deploy cannot move a switch past the Approver. Each replica re-reads the row every
`REFRESH_SECONDS` and applies it to the shared `settings` object: every reader keeps
reading `settings.<name>`, and the loops check their switch on every turn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta

import asyncpg
from pydantic import BaseModel, ConfigDict, Field

from rec.obs import LEARNING_SWITCH
from rec.settings import settings
from rec.store import pg

log = logging.getLogger("learning_settings")
REFRESH_SECONDS = 15
REQUEST_TTL = timedelta(days=7)  # a pending change older than this no longer applies
EXPIRY_ACTOR = "system:learning-settings"


class LearningChanges(BaseModel):
    """The only settings this flow can change, each bounded. Unknown keys are refused."""
    model_config = ConfigDict(extra="forbid")

    auto_retrain_interval_hours: float | None = Field(None, ge=0, le=168)
    auto_retrain_min_new_impressions: int | None = Field(None, ge=1, le=10_000_000)
    auto_retrain_keep_exports: int | None = Field(None, ge=1, le=50)
    online_bandit_enabled: bool | None = None
    online_bandit_exploration: float | None = Field(None, ge=0, le=10)
    promo_holdout_percent: int | None = Field(None, ge=0, le=99)


NAMES = tuple(LearningChanges.model_fields)


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def effective() -> dict:
    return {name: getattr(settings, name) for name in NAMES}


def _apply(values: dict) -> None:
    for name in NAMES:
        if name in values:
            setattr(settings, name, values[name])
    for name, value in effective().items():
        LEARNING_SWITCH.labels(name).set(float(value))


_applied_version: int | None = None


async def refresh(*, force: bool = False) -> bool:
    """Apply the stored values if they changed (or always, with `force`)."""
    global _applied_version
    conn = await pg.pool()
    row = await conn.fetchrow(
        "SELECT settings_values, version FROM learning_settings WHERE id = 1")
    if row is None or (row["version"] == _applied_version and not force):
        return False
    _apply(json.loads(row["settings_values"]))
    _applied_version = row["version"]
    return True


async def loop() -> None:
    while True:
        await asyncio.sleep(REFRESH_SECONDS)
        try:
            if await refresh():
                log.info("learning settings version %s applied", _applied_version)
        except Exception:  # noqa: BLE001 - keep the last applied values and retry
            log.exception("learning settings refresh failed")


async def expire_stale() -> list[str]:
    """Lazily, on every read and write: a request left pending past `REQUEST_TTL` was
    judged on values that may have changed since, so it expires instead of lingering
    (and blocking every other request). Each expiry is audited."""
    conn = await pg.pool()
    rows = await conn.fetch(
        """UPDATE learning_setting_requests SET status = 'EXPIRED', decided_at = now()
           WHERE status = 'PENDING' AND requested_at < now() - $1::interval
           RETURNING request_id, requested_by""", REQUEST_TTL)
    for row in rows:
        await pg.audit(EXPIRY_ACTOR, "System", "learning.change.expire",
                       f"learningSettings/{row['request_id']}", outcome="AUTOMATIC",
                       changes={"requestedBy": row["requested_by"],
                                "ttlDays": REQUEST_TTL.days})
    return [r["request_id"] for r in rows]


async def request_change(changes: LearningChanges, reason: str, actor: str,
                         role: str) -> dict:
    wanted = changes.model_dump(exclude_none=True)
    if not wanted:
        raise Refused(422, "no setting to change")
    await expire_stale()
    request_id = str(uuid.uuid4())
    conn = await pg.pool()
    try:
        await conn.execute(
            """INSERT INTO learning_setting_requests (request_id, changes, reason, requested_by)
               VALUES ($1, $2, $3, $4)""", request_id, json.dumps(wanted), reason, actor)
    except asyncpg.UniqueViolationError:
        raise Refused(409, "a learning settings change is already pending")
    await pg.audit(actor, role, "learning.change.request", f"learningSettings/{request_id}",
                   changes={"changes": wanted, "reason": reason})
    return {"requestId": request_id, "status": "PENDING", "changes": wanted}


async def decide(request_id: str, approve: bool, note: str | None, actor: str,
                 role: str) -> dict:
    await expire_stale()
    conn = await pg.pool()
    async with conn.acquire() as con, con.transaction():
        row = await con.fetchrow(
            "SELECT * FROM learning_setting_requests WHERE request_id = $1 FOR UPDATE",
            request_id)
        if row is None:
            raise Refused(404, "learning settings request not found")
        if row["status"] != "PENDING":
            raise Refused(409, f"request is {row['status']}")
        if row["requested_by"] == actor:
            raise Refused(403, "requester cannot approve their own change")
        before = after = None
        if approve:
            stored = await con.fetchrow(
                "SELECT settings_values FROM learning_settings WHERE id = 1 FOR UPDATE")
            before = json.loads(stored["settings_values"]) if stored else effective()
            after = {**before, **json.loads(row["changes"])}
            # The full set, not just the change: env stops deciding any switch from here.
            await con.execute(
                """INSERT INTO learning_settings (id, settings_values, version, updated_by)
                   VALUES (1, $1, 1, $2)
                   ON CONFLICT (id) DO UPDATE SET settings_values = EXCLUDED.settings_values,
                     version = learning_settings.version + 1,
                     updated_by = EXCLUDED.updated_by, updated_at = now()""",
                json.dumps(after), actor)
        status = "APPROVED" if approve else "REJECTED"
        await con.execute(
            """UPDATE learning_setting_requests SET status = $2, decided_by = $3,
                 decided_at = now(), note = $4 WHERE request_id = $1""",
            request_id, status, actor, note)
    if approve:
        await refresh(force=True)  # this replica now; the others within REFRESH_SECONDS
        # action config.learning: the same row the startup drift check compares against
        await pg.audit(actor, role, "config.learning", "settings/learning",
                       changes={"before": before, "after": after, "requestId": request_id,
                                "requestedBy": row["requested_by"], "reason": row["reason"],
                                "note": note})
    else:
        await pg.audit(actor, role, "learning.change.reject",
                       f"learningSettings/{request_id}", changes={"note": note})
    return {"requestId": request_id, "status": status, "settings": after}


async def overview() -> dict:
    await expire_stale()
    conn = await pg.pool()
    stored = await conn.fetchrow("SELECT * FROM learning_settings WHERE id = 1")
    requests = await conn.fetch(
        "SELECT * FROM learning_setting_requests ORDER BY requested_at DESC LIMIT 10")
    return {
        "effective": effective(),
        "requestTtlDays": REQUEST_TTL.days,
        "source": "approved" if stored else "env",
        "version": stored["version"] if stored else None,
        "updatedBy": stored["updated_by"] if stored else None,
        "updatedAt": stored["updated_at"] if stored else None,
        "requests": [dict(r) | {"changes": json.loads(r["changes"])} for r in requests],
    }
