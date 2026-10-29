"""Record host load average into the sidecar database."""

from __future__ import annotations

import logging

from database.database import Database
from utils.time import now_utc, to_iso
from utils.uptime import format_loadavg, host_loadavg

logger = logging.getLogger(__name__)


async def capture_server_load(db: Database) -> str:
    """Read /proc/loadavg, store the sample, and return the uptime-line text."""
    loads = host_loadavg()
    if loads is None:
        stored = await _latest_stored(db)
        return format_loadavg(stored)
    load_db = db.load_db
    if load_db is not None:
        await load_db.record(to_iso(now_utc()), loads[0], loads[1], loads[2])
    return format_loadavg(loads)


async def _latest_stored(db: Database) -> tuple[float, float, float] | None:
    load_db = db.load_db
    if load_db is None:
        return None
    try:
        return await load_db.latest()
    except Exception:
        logger.exception("Failed to read stored load average")
        return None
