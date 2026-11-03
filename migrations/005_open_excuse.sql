-- Open sick leaves: date_to stays NULL until the leave is closed.
-- Applied when PRAGMA user_version is below 5.

CREATE TABLE excuses_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- sick | leave
    date_from TEXT NOT NULL,
    date_to TEXT,                    -- NULL: open sick leave, no end date yet
    comment TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    cancelled_at TEXT,
    cancelled_by INTEGER,
    FOREIGN KEY (student_id) REFERENCES students(id)
);

INSERT INTO excuses_new (
    id, student_id, kind, date_from, date_to, comment, created_by, created_at, cancelled_at, cancelled_by
)
SELECT
    id, student_id, kind, date_from, date_to, comment, created_by, created_at, cancelled_at, cancelled_by
FROM excuses;

DROP TABLE excuses;

ALTER TABLE excuses_new RENAME TO excuses;

CREATE INDEX IF NOT EXISTS idx_excuses_student ON excuses(student_id, cancelled_at);
