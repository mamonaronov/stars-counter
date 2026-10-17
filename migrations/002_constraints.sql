-- One open request per student and event, and one active holder of each chief role.
-- Applied when PRAGMA user_version is below 2.

DELETE FROM requests
WHERE status = 'open'
  AND id NOT IN (
      SELECT MAX(id) FROM requests WHERE status = 'open' GROUP BY student_id, event_id
  );

CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_one_open
    ON requests(student_id, event_id) WHERE status = 'open';

UPDATE students
SET role = NULL
WHERE role = 'chief_marker'
  AND status = 'active'
  AND id NOT IN (
      SELECT MIN(id) FROM students WHERE role = 'chief_marker' AND status = 'active'
  );

UPDATE students
SET role = NULL
WHERE role = 'chief_admin'
  AND status = 'active'
  AND id NOT IN (
      SELECT MIN(id) FROM students WHERE role = 'chief_admin' AND status = 'active'
  );

CREATE UNIQUE INDEX IF NOT EXISTS idx_students_one_chief_marker
    ON students(role) WHERE role = 'chief_marker' AND status = 'active';

CREATE UNIQUE INDEX IF NOT EXISTS idx_students_one_chief_admin
    ON students(role) WHERE role = 'chief_admin' AND status = 'active';
