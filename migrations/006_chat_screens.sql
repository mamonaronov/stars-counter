-- Last private-chat screen, so a new build can replace it with the current menu.
-- Applied when PRAGMA user_version is below 6.

CREATE TABLE IF NOT EXISTS chat_screens (
    chat_id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
