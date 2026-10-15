from __future__ import annotations

import sqlite3

import pytest

from database.database import Database
from database.queries import Repo
from services.audit import audit
from tests.conftest import actor, make_config


async def test_migration_sets_user_version_and_wal(db: Database):
    assert await db.user_version() == db.config.required_db_version
    async with db.conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    assert row[0].lower() == "wal"


async def test_backup_contains_data_and_rotates(repo: Repo):
    await repo.create_student("Иванов Иван", 2023, "2026-09-01", 1)
    paths = [await repo.db.backup(prefix="t") for _ in range(5)]
    assert all(p.exists() for p in paths[-3:])
    assert not any(p.exists() for p in paths[:2])
    conn = sqlite3.connect(paths[-1])
    try:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
    finally:
        conn.close()


async def test_student_lifecycle(repo: Repo):
    s = await repo.create_student("иванов иван", 2023, "2026-09-01", 1)
    assert s.full_name == "иванов иван"  # normalisation happens in handlers/services
    assert await repo.find_student_by_name("Иванов Иван") is not None
    removed = await repo.remove_student(s.id, "2026-10-01")
    assert removed.status == "removed" and removed.left_on == "2026-10-01"
    assert await repo.list_students() == []
    assert len(await repo.list_students(active_only=False)) == 1
    restored = await repo.restore_student(s.id)
    assert restored.is_active and restored.left_on is None


async def test_verification_link_is_single_use_and_revokes_previous(repo: Repo):
    s = await repo.create_student("Иванов Иван", 2023, "2026-09-01", 1)
    first = await repo.create_verification_link(s.id, 1)
    second = await repo.create_verification_link(s.id, 1)
    assert (await repo.get_verification_link(first.token)).revoked_at is not None
    assert (await repo.get_verification_link(second.token)).revoked_at is None
    await repo.bind_telegram(s.id, 777)
    await repo.use_verification_link(second.token, 777)
    used = await repo.get_verification_link(second.token)
    assert used.used_at is not None and used.used_by == 777
    bound = await repo.get_student_by_telegram(777)
    assert bound is not None and bound.id == s.id and bound.verified_at
    unbound = await repo.unbind_telegram(s.id)
    assert unbound.telegram_id is None and unbound.role is None


async def test_presence_toggle_is_idempotent(repo: Repo):
    s = await repo.create_student("Иванов Иван", 2023, "2026-09-01", 1)
    e = await repo.create_event("Лекция", "mandatory", "2026-09-02", "10:00", None, 1)
    assert await repo.set_presence(e.id, s.id, True, 1) is True
    assert await repo.set_presence(e.id, s.id, True, 1) is False
    assert await repo.is_present(e.id, s.id)
    assert await repo.attendance_counts([e.id]) == {e.id: 1}
    assert await repo.set_presence(e.id, s.id, False, 1) is True
    assert not await repo.is_present(e.id, s.id)
    assert await repo.attendance_all() == {}


async def test_events_order_and_soft_delete(repo: Repo):
    late = await repo.create_event("B", "mandatory", "2026-09-02", "12:00", None, 1)
    early = await repo.create_event("A", "mandatory", "2026-09-02", "09:00", None, 1)
    notime = await repo.create_event("C", "free", "2026-09-02", None, None, 1)
    assert [e.id for e in await repo.list_events()] == [notime.id, early.id, late.id]
    await repo.update_event(late.id, closes_month=1)
    await repo.delete_event(late.id)
    remaining = await repo.list_events()
    assert [e.id for e in remaining] == [notime.id, early.id]
    deleted = await repo.get_event(late.id)
    assert deleted.deleted_at and deleted.closes_month == 0


async def test_requests_and_excuses(repo: Repo):
    s = await repo.create_student("Иванов Иван", 2023, "2026-09-01", 1)
    e = await repo.create_event("Лекция", "mandatory", "2026-09-02", None, None, 1)
    req = await repo.create_request(s.id, e.id, "был")
    assert await repo.has_open_request(s.id, e.id)
    assert await repo.count_open_requests() == 1
    resolved = await repo.resolve_request(req.id, "approved", 1, None)
    assert resolved.status == "approved" and not await repo.has_open_request(s.id, e.id)

    ex = await repo.create_excuse(s.id, "sick", "2026-09-01", "2026-09-05", "справка", 1)
    assert [x.id for x in await repo.list_excuses()] == [ex.id]
    await repo.cancel_excuse(ex.id, 1)
    assert await repo.list_excuses() == []
    assert (await repo.get_excuse(ex.id)).cancelled_by == 1


async def test_audit_records_actor_role(repo: Repo):
    a = actor("chief_marker")
    await audit(repo, a, "student.add", entity_type="student", entity_id=5, студент="Иванов Иван")
    entries = await repo.list_audit(limit=10)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.actor_role == "chief_marker"
    assert entry.actor_telegram_id == a.telegram_id
    assert entry.actor_name == "Отмечающий Тест"
    assert "Иванов Иван" in (entry.details or "")
    assert await repo.count_audit(entity_type="student", entity_id=5) == 1


async def test_callback_dedup(repo: Repo):
    assert await repo.claim_callback("abc", 1)
    assert not await repo.claim_callback("abc", 1)
    await repo.cleanup_callbacks("2999-01-01T00:00:00+00:00")
    assert await repo.claim_callback("abc", 1)


async def test_damaged_db_restored_from_backup(tmp_path):
    config = make_config(tmp_path)
    db = Database(config)
    await db.initialize()
    repo = Repo(db)
    await repo.create_student("Иванов Иван", 2023, "2026-09-01", 1)
    await db.backup(prefix="good")
    await db.close()
    config.db_path.write_bytes(b"garbage" * 1000)
    for suffix in ("-wal", "-shm"):
        p = config.db_path.with_name(config.db_path.name + suffix)
        if p.exists():
            p.unlink()
    db2 = Database(config)
    await db2.initialize()
    try:
        assert len(await Repo(db2).list_students()) == 1
    finally:
        await db2.close()
    assert any(p.name.startswith("database.sqlite3.damaged.") for p in tmp_path.iterdir())
