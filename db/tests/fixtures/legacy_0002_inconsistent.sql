-- Adds a 0002 response whose stored score contradicts the answer key. 0003 must refuse it.
SET session_replication_role = replica;
INSERT INTO responses (question_id, user_id, user_name, response_answer, is_correct, responded_at)
VALUES (5, 'bad@alrium.com', 'Bad Score', 'A', true, '2026-10-22 10:00:00+05:30');
SET session_replication_role = origin;
