-- Prayer track (молитва / тфилин клуб), column G of the attendance journal.
-- Applied when PRAGMA user_version is below 4.

ALTER TABLE students ADD COLUMN track TEXT;
