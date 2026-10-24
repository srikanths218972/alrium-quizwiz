-- Adds a legacy response whose stored score contradicts the answer key.
-- The 0002 upgrade must refuse it and roll back.
SET session_replication_role = replica;
INSERT INTO responses (question_id, user_id, user_name, response_answer, is_correct, responded_at)
VALUES ('33333333-3333-3333-3333-333333333333', 'bad@example.com', 'Bad Score', 'A', true, '2026-09-03 10:00:00+05:30');
SET session_replication_role = origin;
