"""Separate SQLite file for ping samples. Not included in backups."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from config import Config

logger = logging.getLogger(__name__)

VPN_DB_VERSION = 1
# After a trim, sit below the cap so the next samples do not vacuum immediately.
_TARGET_RATIO = 0.90

_VPN_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vpn_latency_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    latency_ms INTEGER,
    node_name TEXT,
    subscription TEXT,
    error TEXT,
    host_uptime_s REAL
);

CREATE INDEX IF NOT EXISTS idx_vpn_latency_measured_at
    ON vpn_latency_samples(measured_at);

CREATE INDEX IF NOT EXISTS idx_vpn_latency_sub_time
    ON vpn_latency_samples(subscription, measured_at);
"""


def resolve_vpn_db_path(config: Config) -> Path:
    path = getattr(config, "vpn_db_path", None)
    if path:
        return Path(path)
    return Path(config.db_path).with_name("vpn.sqlite3")


def sqlite_bundle_bytes(path: Path) -> int:
    """Bytes on disk: main file plus WAL and shared-memory sidecars."""
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


class VpnDatabase:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.path = resolve_vpn_db_path(config)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            from database.database import DatabaseError

            raise DatabaseError("VPN database is not connected")
        return self._conn

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._apply_pragmas()
        await self.conn.executescript(_VPN_SCHEMA_SQL)
        await self.conn.execute(f"PRAGMA user_version={VPN_DB_VERSION}")
        await self.conn.commit()
        logger.info("VPN database ready path=%s", self.path)

    async def close(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logger.exception("VPN WAL checkpoint failed during close")
        await self._conn.close()
        self._conn = None

    async def _apply_pragmas(self) -> None:
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=30000")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA temp_store=MEMORY")
        await self.conn.execute("PRAGMA wal_autocheckpoint=1000")
        await self.conn.commit()

    async def vacuum(self) -> None:
        await self.conn.commit()
        await self.conn.execute("VACUUM")

    async def _checkpoint(self) -> None:
        await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await self.conn.commit()

    async def _pragma_int(self, name: str) -> int:
        async with self.conn.execute(f"PRAGMA {name}") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _live_bytes(self) -> int:
        page_size = await self._pragma_int("page_size")
        page_count = await self._pragma_int("page_count")
        freelist = await self._pragma_int("freelist_count")
        return max(0, page_count - freelist) * page_size

    async def _count(self) -> int:
        async with self.conn.execute("SELECT COUNT(*) FROM vpn_latency_samples") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def _delete_oldest(self, n: int) -> int:
        if n <= 0:
            return 0
        before = await self._count()
        await self.conn.execute(
            """
            DELETE FROM vpn_latency_samples
            WHERE id IN (
                SELECT id FROM vpn_latency_samples
                ORDER BY measured_at ASC, id ASC
                LIMIT ?
            )
            """,
            (n,),
        )
        await self.conn.commit()
        return before - await self._count()

    async def enforce_max_bytes(self, max_bytes: int) -> int:
        """Drop the oldest samples until the file, WAL and shm stay within *max_bytes*."""
        if max_bytes <= 0:
            return 0
        await self._checkpoint()
        if sqlite_bundle_bytes(self.path) <= max_bytes:
            return 0

        deleted = 0
        for _ in range(6):
            if sqlite_bundle_bytes(self.path) <= max_bytes:
                break
            live = await self._live_bytes()
            reserve = min(64 * 1024, max_bytes // 10)
            target = max(4096, int(max_bytes * _TARGET_RATIO) - reserve)
            if live > target:
                count = await self._count()
                if count <= 1 or live <= 0:
                    await self.vacuum()
                    await self._checkpoint()
                    break
                keep = max(1, int(count * target / live))
                if keep >= count:
                    keep = count - 1
                removed = await self._delete_oldest(count - keep)
                deleted += removed
                if removed <= 0:
                    await self.vacuum()
                    await self._checkpoint()
                    break
            await self.vacuum()
            await self._checkpoint()

        size = sqlite_bundle_bytes(self.path)
        if size > max_bytes:
            logger.warning(
                "VPN database still above cap after prune path=%s size=%s cap=%s",
                self.path,
                size,
                max_bytes,
            )
        elif deleted:
            logger.info(
                "VPN samples pruned=%s size=%s cap=%s path=%s",
                deleted,
                size,
                max_bytes,
                self.path,
            )
        return deleted
