"""Separate SQLite file for server load average samples. Not included in backups."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import aiosqlite

from config import Config
from utils.time import now_utc, to_iso

logger = logging.getLogger(__name__)

LOAD_DB_VERSION = 1
_LOAD_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS load_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at TEXT NOT NULL,
    load1 REAL NOT NULL,
    load5 REAL NOT NULL,
    load15 REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_load_sampled_at
    ON load_samples(sampled_at);
"""


def resolve_load_db_path(config: Config) -> Path:
    path = getattr(config, "load_db_path", None)
    if path:
        return Path(path)
    return Path(config.db_path).with_name("load.sqlite3")


async def _vacuum(conn: aiosqlite.Connection) -> None:
    await conn.commit()
    await conn.execute("VACUUM")


def load_retention_cutoff(keep_days: int) -> str | None:
    if keep_days <= 0:
        return None
    return to_iso(now_utc() - timedelta(days=keep_days))


class LoadDatabase:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.path = resolve_load_db_path(config)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            from database.database import DatabaseError

            raise DatabaseError("Load database is not connected")
        return self._conn

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._apply_pragmas()
        await self.conn.executescript(_LOAD_SCHEMA_SQL)
        await self.conn.execute(f"PRAGMA user_version={LOAD_DB_VERSION}")
        await self.conn.commit()
        logger.info("Load database ready path=%s", self.path)

    async def close(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logger.exception("Load WAL checkpoint failed during close")
        await self._conn.close()
        self._conn = None

    async def _apply_pragmas(self) -> None:
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA temp_store=MEMORY")
        await self.conn.execute("PRAGMA wal_autocheckpoint=1000")
        await self.conn.commit()

    async def record(
        self, sampled_at: str, load1: float, load5: float, load15: float
    ) -> int:
        cur = await self.conn.execute(
            """
            INSERT INTO load_samples (sampled_at, load1, load5, load15)
            VALUES (?, ?, ?, ?)
            """,
            (sampled_at, load1, load5, load15),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def list_between(self, start_iso: str, end_iso: str) -> list[tuple[str, float, float, float]]:
        async with self.conn.execute(
            """
            SELECT sampled_at, load1, load5, load15
            FROM load_samples
            WHERE sampled_at >= ? AND sampled_at < ?
            ORDER BY sampled_at ASC, id ASC
            """,
            (start_iso, end_iso),
        ) as cur:
            rows = await cur.fetchall()
        return [
            (str(row["sampled_at"]), float(row["load1"]), float(row["load5"]), float(row["load15"]))
            for row in rows
        ]

    async def earliest_at(self) -> str | None:
        async with self.conn.execute(
            "SELECT sampled_at FROM load_samples ORDER BY sampled_at ASC, id ASC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return str(row["sampled_at"]) if row else None

    async def latest(self) -> tuple[float, float, float] | None:
        async with self.conn.execute(
            """
            SELECT load1, load5, load15
            FROM load_samples
            ORDER BY sampled_at DESC, id DESC
            LIMIT 1
            """
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return (float(row["load1"]), float(row["load5"]), float(row["load15"]))

    async def prune_older_than(self, cutoff_iso: str) -> int:
        cur = await self.conn.execute(
            "DELETE FROM load_samples WHERE sampled_at < ?",
            (cutoff_iso,),
        )
        deleted = int(cur.rowcount or 0)
        await self.conn.commit()
        return deleted

    async def prune_retained(self, keep_days: int, *, vacuum: bool = True) -> int:
        cutoff = load_retention_cutoff(keep_days)
        if cutoff is None:
            return 0
        deleted = await self.prune_older_than(cutoff)
        if deleted and vacuum:
            await _vacuum(self.conn)
            logger.info("Load samples pruned=%s vacuumed path=%s", deleted, self.path)
        elif deleted:
            logger.info("Load samples pruned=%s path=%s", deleted, self.path)
        return deleted
