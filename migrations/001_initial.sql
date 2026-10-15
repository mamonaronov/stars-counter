-- Initial schema for the attendance bot.
-- Applied when PRAGMA user_version is below 1.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS system_info (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fsm_storage (
    bot_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    destiny TEXT NOT NULL DEFAULT 'default',
    state TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, chat_id, user_id, destiny)
);

CREATE TABLE IF NOT EXISTS callback_dedup (
    callback_id TEXT PRIMARY KEY,
    telegram_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

-- Every Telegram account that ever talked to the bot.
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

-- Students of the group. telegram_id is set after verification.
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    enrollment_year INTEGER,
    telegram_id INTEGER UNIQUE,
    verified_at TEXT,
    role TEXT,                       -- marker | chief_marker | admin | chief_admin | NULL
    joined_on TEXT NOT NULL,         -- events on/after this date count for the student
    left_on TEXT,                    -- set when the student is removed
    status TEXT NOT NULL DEFAULT 'active',  -- active | removed
    created_by INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_students_status_name ON students(status, full_name);

CREATE TABLE IF NOT EXISTS verification_links (
    token TEXT PRIMARY KEY,
    student_id INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    used_at TEXT,
    used_by INTEGER,
    revoked_at TEXT,
    FOREIGN KEY (student_id) REFERENCES students(id)
);

CREATE INDEX IF NOT EXISTS idx_verification_student ON verification_links(student_id);

CREATE TABLE IF NOT EXISTS event_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,              -- mandatory | free
    default_time TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,              -- mandatory | free
    event_date TEXT NOT NULL,        -- YYYY-MM-DD
    start_time TEXT,                 -- HH:MM or NULL
    template_id INTEGER,
    closes_month INTEGER NOT NULL DEFAULT 0,
    month_title TEXT,                -- custom name of the period this event closes
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    FOREIGN KEY (template_id) REFERENCES event_templates(id)
);

CREATE INDEX IF NOT EXISTS idx_events_order ON events(deleted_at, event_date, start_time, id);

CREATE TABLE IF NOT EXISTS attendance (
    event_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    marked_by INTEGER NOT NULL,
    marked_at TEXT NOT NULL,
    PRIMARY KEY (event_id, student_id),
    FOREIGN KEY (event_id) REFERENCES events(id),
    FOREIGN KEY (student_id) REFERENCES students(id)
);

CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance(student_id);

-- Sick leaves and approved absences.
CREATE TABLE IF NOT EXISTS excuses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- sick | leave
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    comment TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    cancelled_at TEXT,
    cancelled_by INTEGER,
    FOREIGN KEY (student_id) REFERENCES students(id)
);

CREATE INDEX IF NOT EXISTS idx_excuses_student ON excuses(student_id, cancelled_at);

-- "You forgot to mark me" requests from verified students.
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',   -- open | approved | rejected
    created_at TEXT NOT NULL,
    resolved_by INTEGER,
    resolved_at TEXT,
    resolution_comment TEXT,
    FOREIGN KEY (student_id) REFERENCES students(id),
    FOREIGN KEY (event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status, created_at);

-- Who did what, when, and with which rights at that moment.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_telegram_id INTEGER NOT NULL,
    actor_student_id INTEGER,
    actor_name TEXT,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
