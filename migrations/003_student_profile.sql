-- Direction and Judaica group, shown in the attendance journal.
-- Applied when PRAGMA user_version is below 3.

ALTER TABLE students ADD COLUMN direction TEXT;
ALTER TABLE students ADD COLUMN judaica_group TEXT;
