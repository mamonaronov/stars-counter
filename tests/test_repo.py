from __future__ import annotations

import sqlite3

import pytest

from database.database import Database, DatabaseError, split_sql
from database.queries import Approval, MonthBoundaryError, Repo, RoleOccupiedError, RoleTransferError
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
    assert resolved is not None and resolved.status == "approved" and not await repo.has_open_request(s.id, e.id)
    assert await repo.resolve_request(req.id, "rejected", 2, "поздно") is None
    assert (await repo.get_request(req.id)).status == "approved"

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


async def test_names_sort_with_cyrillic_casefold(repo: Repo):
    yakov = await repo.create_student("Яковлев Яков", 2023, "2026-09-01", 1)
    anna = await repo.create_student("антонов антон", 2023, "2026-09-01", 1)
    await repo.bind_telegram(yakov.id, 11)
    await repo.bind_telegram(anna.id, 12)
    await repo.set_role(yakov.id, "marker")
    await repo.set_role(anna.id, "marker")
    assert [s.id for s in await repo.list_students_with_roles()] == [anna.id, yakov.id]
    assert [s.id for s in await repo.list_verified_students()] == [anna.id, yakov.id]
    later = await repo.create_template("ярмарка", "free", None, 1)
    earlier = await repo.create_template("Актовый", "mandatory", None, 1)
    assert [item.id for item in await repo.list_templates()] == [earlier.id, later.id]


async def test_verification_redeem_does_not_overwrite_another_account(repo: Repo):
    student = await repo.create_student("Иванов Иван", 2023, "2026-09-01", 1)
    other = await repo.create_student("Петров Пётр", 2023, "2026-09-01", 1)
    first = await repo.create_verification_link(student.id, 1)
    ok = await repo.redeem_verification(first.token, 777)
    assert ok.code == "ok" and ok.student is not None and ok.student.telegram_id == 777
    assert (await repo.redeem_verification(first.token, 778)).code == "used"
    second = await repo.create_verification_link(student.id, 1)
    assert (await repo.redeem_verification(second.token, 778)).code == "taken"
    assert (await repo.get_student(student.id)).telegram_id == 777
    foreign = await repo.create_verification_link(other.id, 1)
    linked = await repo.redeem_verification(foreign.token, 777)
    assert linked.code == "linked_other" and linked.student is not None and linked.student.id == student.id
    assert (await repo.get_student(other.id)).telegram_id is None
    assert (await repo.get_verification_link(foreign.token)).used_at is None


async def test_chief_role_is_unique_and_transfer_is_atomic(repo: Repo):
    chief = await repo.create_student("Главный Отмечающий", 2023, "2026-09-01", 1)
    marker = await repo.create_student("Обычный Отмечающий", 2023, "2026-09-01", 1)
    removed = await repo.create_student("Удалённый Студент", 2023, "2026-09-01", 1)
    await repo.bind_telegram(chief.id, 10)
    await repo.bind_telegram(marker.id, 11)
    await repo.bind_telegram(removed.id, 12)
    await repo.set_role(chief.id, "chief_marker")
    await repo.remove_student(removed.id, "2026-10-01")
    with pytest.raises(RoleOccupiedError):
        await repo.set_role(marker.id, "chief_marker")
    with pytest.raises(RoleTransferError):
        await repo.transfer_role(chief.id, removed.id, "chief_marker", "marker")
    assert (await repo.get_student(chief.id)).role == "chief_marker"
    await repo.transfer_role(chief.id, marker.id, "chief_marker", "marker")
    assert (await repo.get_student(marker.id)).role == "chief_marker"
    assert (await repo.get_student(chief.id)).role == "marker"


async def test_toggle_presence_and_request_approval_are_atomic(repo: Repo):
    student = await repo.create_student("Иванов Иван", 2023, "2026-09-01", 1)
    event = await repo.create_event("Лекция", "mandatory", "2026-09-02", "10:00", None, 1)
    changed, present = await repo.toggle_presence(event.id, student.id, 1, allow_mark=False)
    assert (changed, present) == (False, False)
    changed, present = await repo.toggle_presence(event.id, student.id, 1, allow_mark=True)
    assert (changed, present) == (True, True)
    changed, present = await repo.toggle_presence(event.id, student.id, 1, allow_mark=True)
    assert (changed, present) == (True, False)

    req = await repo.create_request(student.id, event.id, "был")
    assert req is not None
    assert await repo.create_request(student.id, event.id, "ещё раз") is None
    closed = await repo.list_requests(status=None, closed_only=True)
    assert closed == []
    approval = await repo.approve_request_with_attendance(req.id, 5)
    assert isinstance(approval, Approval)
    assert approval.code == "ok" and approval.marked and approval.request is not None
    assert await repo.is_present(event.id, student.id)
    assert (await repo.approve_request_with_attendance(req.id, 6)).code == "resolved"
    assert [item.id for item in await repo.list_requests(status=None, closed_only=True)] == [req.id]
    assert await repo.list_requests(status="open") == []


async def test_failed_migration_does_not_bump_version(tmp_path):
    config = make_config(tmp_path)
    db = Database(config)
    await db.connect()
    try:
        with pytest.raises(DatabaseError):
            await db._apply_migration_unlocked(9, "CREATE TABLE boom(id INTEGER);\nNOT SQL;")
        assert await db.user_version() == 0
        async with db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'boom'") as cur:
            assert await cur.fetchone() is None
    finally:
        await db.close()


async def test_upgrade_from_v1_collapses_duplicates(tmp_path):
    from database.database import MIGRATIONS_DIR

    config = make_config(tmp_path)
    db = Database(config)
    await db.connect()
    try:
        await db._apply_migration_unlocked(1, (MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8"))
        await db.conn.executescript(
            """
            INSERT INTO students (full_name, joined_on, status, role, telegram_id, created_at, updated_at) VALUES
             ('А', '2026-09-01', 'active', 'chief_marker', 1, 't', 't'),
             ('Б', '2026-09-01', 'active', 'chief_marker', 2, 't', 't');
            INSERT INTO events (title, kind, event_date, created_by, created_at, updated_at) VALUES
             ('Лекция', 'mandatory', '2026-09-02', 1, 't', 't');
            INSERT INTO requests (student_id, event_id, text, status, created_at) VALUES
             (1, 1, 'old', 'open', 't1'),
             (1, 1, 'new', 'open', 't2');
            """
        )
        await db.migrate()
        assert await db.user_version() == 2
        async with db.conn.execute("SELECT id, role FROM students ORDER BY id") as cur:
            assert [tuple(row) for row in await cur.fetchall()] == [(1, "chief_marker"), (2, None)]
        async with db.conn.execute("SELECT text FROM requests WHERE status = 'open'") as cur:
            assert [row[0] for row in await cur.fetchall()] == ["new"]
    finally:
        await db.close()


def test_split_sql_keeps_statements_and_drops_comments():
    statements = split_sql("-- comment\nCREATE TABLE t(id INTEGER);\n\nINSERT INTO t VALUES (1);")
    assert statements == ["CREATE TABLE t(id INTEGER)", "INSERT INTO t VALUES (1)"]


async def test_month_boundary_and_import_are_atomic(repo: Repo):
    first = await repo.create_event("Первое", "mandatory", "2026-09-01", None, None, 1)
    second = await repo.create_event("Второе", "mandatory", "2026-09-10", None, None, 1)
    await repo.update_event(first.id, closes_month=1, month_title="Сентябрь")
    await repo.set_month_closer(second.id, previous_closer_id=first.id, month_title="Сентябрь")
    assert (await repo.get_event(first.id)).closes_month == 0
    assert (await repo.get_event(second.id)).closes_month == 1
    assert (await repo.get_event(second.id)).month_title == "Сентябрь"
    with pytest.raises(MonthBoundaryError):
        await repo.set_month_closer(first.id, previous_closer_id=999, month_title="Октябрь")
    assert (await repo.get_event(second.id)).closes_month == 1
    assert (await repo.get_event(first.id)).closes_month == 0

    await repo.delete_event(second.id, successor_id=first.id, month_title="Сентябрь")
    kept = await repo.get_event(first.id)
    removed = await repo.get_event(second.id)
    assert kept is not None and kept.closes_month == 1 and kept.month_title == "Сентябрь"
    assert removed is not None and removed.deleted_at and removed.closes_month == 0
    with pytest.raises(MonthBoundaryError):
        await repo.delete_event(first.id, successor_id=second.id, month_title="Сентябрь")
    assert (await repo.get_event(first.id)).deleted_at is None

    created = await repo.import_students([("Иванов Иван", 2023), ("Петров Пётр", 2024)], "2026-09-01", 1)
    assert [student.full_name for student in created] == ["Иванов Иван", "Петров Пётр"]
    again = await repo.import_students([("иванов иван", 2023), ("Сидоров Сидор", 2022)], "2026-09-01", 1)
    assert [student.full_name for student in again] == ["Сидоров Сидор"]
    assert len(await repo.list_students()) == 3


async def test_set_system_raises(tmp_path):
    db = Database(make_config(tmp_path))
    await db.initialize()
    try:
        await db.set_system("probe", "1")
        assert await db.get_system("probe") == "1"
        await db.conn.close()
        with pytest.raises(Exception):
            await db.set_system("probe", "2")
        with pytest.raises(Exception):
            await db.get_system("probe")
    finally:
        db._conn = None
        await db.close()


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


async def test_wipe_all_data_keeps_schema_and_config(repo: Repo):
    student = await repo.create_student("Иванов Иван", 2023, "2026-09-01", 1)
    event = await repo.create_event("Лекция", "mandatory", "2026-09-01", "10:00", None, 1)
    await repo.set_presence(event.id, student.id, True, 1)
    await repo.touch_user(7, "user", "Имя", None)
    await repo.db.set_system("markers_group_id", "-100")
    await repo.insert_vpn_sample("2026-10-01T00:00:00+00:00", True, 12, "node", "sub", None, 1.0)
    backup = await repo.db.backup(prefix="before_wipe")
    version = await repo.db.user_version()
    config_id = repo.db.config.superadmin_id

    await repo.db.wipe_all_data()

    assert await repo.list_students(active_only=False) == []
    assert await repo.list_events() == []
    assert await repo.get_user(7) is None
    assert await repo.db.get_system("markers_group_id") is None
    assert await repo.count_vpn_samples() == 0
    assert not backup.exists()
    assert repo.db.latest_backup() is None
    assert await repo.db.user_version() == version
    assert repo.db.config.superadmin_id == config_id
    async with repo.db.conn.execute("PRAGMA foreign_keys") as cur:
        fk = await cur.fetchone()
    assert int(fk[0]) == 1
    async with repo.db.conn.execute("PRAGMA journal_mode") as cur:
        mode = await cur.fetchone()
    assert str(mode[0]).lower() == "wal"
    with pytest.raises(sqlite3.IntegrityError):
        await repo.db.conn.execute(
            "INSERT INTO attendance (event_id, student_id, marked_by, marked_at) VALUES (1, 1, 1, 't')"
        )
    again = await repo.create_student("Новый Студент", 2024, "2026-09-01", 1)
    assert again.id == 1
