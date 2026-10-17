"""SQLite engine: WAL, migrations, integrity, backup and restore."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from config import Config
from database.vpn_database import VpnDatabase
from utils.time import now_utc, to_iso

logger = logging.getLogger(__name__)

_BACKUP_TIMEOUT_SECONDS = 60.0
_BACKUP_CLOSE_TIMEOUT_SECONDS = 2.0

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class DatabaseError(RuntimeError):
    pass


class DatabaseUnrecoverableError(DatabaseError):
    pass


def _backup_name(prefix: str = "backup") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{stamp}.sqlite3"


def list_sqlite_backups(backup_dir: Path) -> list[Path]:
    if not backup_dir.is_dir():
        return []
    files = [p for p in backup_dir.glob("*.sqlite3") if p.is_file()]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)


def sqlite_sidecar(path: Path, suffix: str) -> Path:
    return Path(str(path) + suffix)


def clear_sqlite_sidecars(db_path: Path) -> None:
    sqlite_sidecar(db_path, "-wal").unlink(missing_ok=True)
    sqlite_sidecar(db_path, "-shm").unlink(missing_ok=True)


def copy_sqlite_bundle(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    for suffix in ("-wal", "-shm"):
        extra = sqlite_sidecar(src, suffix)
        if extra.is_file():
            shutil.copy2(extra, sqlite_sidecar(dest, suffix))


def split_sql(script: str) -> list[str]:
    """Split a migration script into statements. Line comments start with ``--``."""
    statements: list[str] = []
    chunk: list[str] = []
    for line in script.splitlines():
        code = line.split("--", 1)[0]
        if not code.strip():
            continue
        chunk.append(code)
        if code.rstrip().endswith(";"):
            statement = " ".join(part.strip() for part in chunk).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            chunk = []
    tail = " ".join(part.strip() for part in chunk).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def install_sqlite_file(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".incoming")
    try:
        shutil.copy2(source, tmp)
        clear_sqlite_sidecars(dest)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


class Database:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.path = config.db_path
        self.backup_dir = config.backup_path
        self._conn: aiosqlite.Connection | None = None
        self.vpn_db: VpnDatabase | None = None
        # One connection, many tasks. Hold this across every statement and across
        # multi-statement transactions so a rollback cannot discard another task's write.
        self._lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise DatabaseError("Database is not connected")
        return self._conn

    @asynccontextmanager
    async def exclusive(self):
        """Serialize every use of the main connection."""
        async with self._lock:
            yield

    async def _rollback(self) -> None:
        try:
            await self.conn.rollback()
        except Exception:
            logger.debug("SQLite rollback skipped", exc_info=True)

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        # Autocommit: transactions start only at an explicit BEGIN.
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        try:
            await self._apply_pragmas(conn)
        except Exception:
            # A garbage file fails already on PRAGMA; the worker thread must not leak.
            await conn.close()
            raise
        self._conn = conn
        await self._assert_wal()

    async def close(self) -> None:
        if self.vpn_db is not None:
            try:
                await self.vpn_db.close()
            except Exception:
                logger.exception("VPN database close failed")
            self.vpn_db = None
        if self._conn is not None:
            try:
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                logger.exception("WAL checkpoint failed during close")
            await self._conn.close()
            self._conn = None

    async def _apply_pragmas(self, conn: aiosqlite.Connection) -> None:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA temp_store=MEMORY")
        await conn.commit()

    async def _assert_wal(self) -> None:
        async with self.conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        mode = (row[0] if row else "").lower()
        if mode != "wal":
            raise DatabaseError(f"SQLite journal_mode is {mode!r}, expected WAL")
        logger.info("SQLite WAL mode confirmed")

    async def integrity_ok(self, conn: aiosqlite.Connection | None = None) -> bool:
        target = conn or self.conn
        try:
            async with target.execute("PRAGMA integrity_check") as cur:
                row = await cur.fetchone()
            return bool(row) and str(row[0]).lower() == "ok"
        except Exception:
            logger.exception("integrity_check failed")
            return False

    async def _user_version_unlocked(self) -> int:
        async with self.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def user_version(self) -> int:
        async with self._lock:
            return await self._user_version_unlocked()

    async def set_user_version(self, version: int) -> None:
        async with self._lock:
            await self.conn.execute(f"PRAGMA user_version={int(version)}")
            await self.conn.commit()

    def _migration_files(self) -> list[tuple[int, Path]]:
        files: list[tuple[int, Path]] = []
        if not MIGRATIONS_DIR.exists():
            return files
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            prefix = path.name.split("_", 1)[0]
            if prefix.isdigit():
                files.append((int(prefix), path))
        return files

    async def _apply_migration_unlocked(self, version: int, sql: str) -> None:
        """Apply one script and bump user_version in a single transaction."""
        try:
            async with self.conn.execute("BEGIN IMMEDIATE"):
                pass
            for statement in split_sql(sql):
                if statement.upper().startswith("PRAGMA FOREIGN_KEYS"):
                    continue
                await self.conn.execute(statement)
            await self.conn.execute(f"PRAGMA user_version={int(version)}")
            await self.conn.commit()
        except Exception as exc:
            await self._rollback()
            logger.exception("Migration %s failed", version)
            raise DatabaseError(f"Migration {version} failed") from exc

    async def migrate(self) -> None:
        async with self._lock:
            current = await self._user_version_unlocked()
            required = self.config.required_db_version
            logger.info("DB version current=%s required=%s", current, required)
            if current > required:
                raise DatabaseError(f"Database version {current} is newer than application {required}")
            if current == required:
                return
            files = [(ver, path) for ver, path in self._migration_files() if ver > current]
            if not files:
                raise DatabaseError(f"No migrations to go from {current} to {required}")
        if current > 0:
            await self.backup(prefix="pre_migrate")
        async with self._lock:
            now = await self._user_version_unlocked()
            if now != current:
                raise DatabaseError(f"Database version changed from {current} to {now} before migration")
            for version, path in files:
                sql = path.read_text(encoding="utf-8")
                logger.info("Applying migration %s", path.name)
                await self._apply_migration_unlocked(version, sql)
            final = await self._user_version_unlocked()
            if final < required:
                raise DatabaseError(f"Migrations finished at {final}, required {required}")
            logger.info("Migrations complete, version=%s", final)

    async def _close_backup_conn(self, conn: aiosqlite.Connection | None) -> None:
        if conn is None:
            return
        try:
            await asyncio.wait_for(conn.close(), timeout=_BACKUP_CLOSE_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("Backup connection close timed out or failed")

    async def backup_to(self, dest_path: Path) -> Path:
        """Write a consistent SQLite snapshot (includes WAL pages) via a side connection."""
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_name(dest_path.name + ".tmp")
        src: aiosqlite.Connection | None = None
        dest: aiosqlite.Connection | None = None
        try:
            src = await aiosqlite.connect(self.path)
            dest = await aiosqlite.connect(tmp_path)
            await asyncio.wait_for(src.backup(dest), timeout=_BACKUP_TIMEOUT_SECONDS)
            await self._close_backup_conn(dest)
            dest = None
            await self._close_backup_conn(src)
            src = None
            tmp_path.replace(dest_path)
            return dest_path
        except TimeoutError as exc:
            tmp_path.unlink(missing_ok=True)
            raise DatabaseError(f"SQLite backup timed out after {_BACKUP_TIMEOUT_SECONDS:.0f}s") from exc
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        finally:
            await self._close_backup_conn(dest)
            await self._close_backup_conn(src)

    async def backup(self, prefix: str = "backup") -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest_path = self.backup_dir / _backup_name(prefix)
        try:
            await self.backup_to(dest_path)
        except Exception:
            dest_path.unlink(missing_ok=True)
            logger.exception("Backup failed")
            raise
        try:
            await self.set_system("last_backup_at", to_iso(now_utc()))
            await self.set_system("last_backup_path", str(dest_path))
        except Exception:
            logger.exception("Backup %s is on disk, but system_info was not updated", dest_path.name)
            raise
        logger.info("Backup created: %s", dest_path.name)
        self._rotate_backups()
        return dest_path

    def _rotate_backups(self) -> None:
        files = list_sqlite_backups(self.backup_dir)
        keep = max(1, self.config.backup_keep)
        for stale in files[keep:]:
            try:
                stale.unlink()
                logger.info("Rotated old backup %s", stale.name)
            except OSError:
                logger.exception("Failed to remove old backup %s", stale)

    def latest_backup(self) -> Path | None:
        files = list_sqlite_backups(self.backup_dir)
        return files[0] if files else None

    async def get_system(self, key: str) -> str | None:
        async with self._lock:
            try:
                async with self.conn.execute("SELECT value FROM system_info WHERE key = ?", (key,)) as cur:
                    row = await cur.fetchone()
            except Exception:
                logger.exception("Failed to read system_info key %s", key)
                raise
        return str(row[0]) if row and row[0] is not None else None

    async def set_system(self, key: str, value: str) -> None:
        async with self._lock:
            try:
                await self.conn.execute(
                    """
                    INSERT INTO system_info(key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (key, value, to_iso(now_utc())),
                )
                await self.conn.commit()
            except Exception:
                logger.exception("Failed to write system_info key %s", key)
                await self._rollback()
                raise

    async def quarantine_damaged(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        damaged = self.path.with_name(f"{self.path.name}.damaged.{stamp}")
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        copy_sqlite_bundle(self.path, damaged)
        logger.critical("Damaged database copied to %s", damaged)
        return damaged

    async def _user_tables_unlocked(self) -> list[str]:
        async with self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    async def wipe_all_data(self) -> None:
        """Delete every stored row and on-disk backup. Schema, user_version and config stay."""
        async with self._lock:
            tables = await self._user_tables_unlocked()
            for name in tables:
                if not _TABLE_NAME.fullmatch(name):
                    raise DatabaseError(f"Unexpected table name {name!r}")
            await self.conn.execute("PRAGMA foreign_keys=OFF")
            try:
                async with self.conn.execute("BEGIN IMMEDIATE"):
                    pass
                for name in tables:
                    await self.conn.execute(f"DELETE FROM {name}")
                async with self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
                ) as cur:
                    has_sequence = await cur.fetchone()
                if has_sequence:
                    await self.conn.execute("DELETE FROM sqlite_sequence")
                await self.conn.commit()
            except Exception:
                await self._rollback()
                logger.exception("Failed to wipe database %s", self.path)
                raise
            finally:
                await self.conn.execute("PRAGMA foreign_keys=ON")
            await self.conn.execute("VACUUM")
            if not await self.integrity_ok():
                raise DatabaseError("Database failed integrity_check after wipe")
        if self.vpn_db is not None:
            await self.vpn_db.wipe_samples()
        self._clear_backup_files()
        logger.warning("Runtime data wiped path=%s", self.path)

    def _clear_backup_files(self) -> None:
        if not self.backup_dir.is_dir():
            return
        for path in self.backup_dir.iterdir():
            if not path.is_file():
                continue
            try:
                path.unlink()
            except OSError:
                logger.exception("Failed to remove backup file %s", path)

    async def restore_from(self, backup_path: Path) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        install_sqlite_file(backup_path, self.path)
        await self.connect()
        if not await self.integrity_ok():
            raise DatabaseUnrecoverableError("Restored backup failed integrity_check")
        logger.info("Database restored from %s", backup_path.name)

    async def initialize(self) -> None:
        """Connect, repair if needed, migrate, confirm WAL."""
        healthy = True
        try:
            await self.connect()
            if self.path.exists() and self.path.stat().st_size > 0:
                healthy = await self.integrity_ok()
        except sqlite3.DatabaseError as exc:
            logger.critical("Primary database cannot be opened: %s", exc)
            healthy = False
        if not healthy:
            logger.critical("Primary database failed integrity_check")
            await self.quarantine_damaged()
            backup = self.latest_backup()
            if backup is None:
                raise DatabaseUnrecoverableError("Database damaged and no backup is available")
            await self.restore_from(backup)
        await self.migrate()
        if not await self.integrity_ok():
            raise DatabaseUnrecoverableError("Database failed integrity_check after migrate")
        await self._assert_wal()
        await self._init_vpn_database()

    async def _init_vpn_database(self) -> None:
        if self.vpn_db is None:
            self.vpn_db = VpnDatabase(self.config)
            await self.vpn_db.initialize()
        await self.vpn_db.enforce_max_bytes(self.config.vpn_db_max_bytes)
