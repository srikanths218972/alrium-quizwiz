-- Populated 0002 (integer IDs + TEXT option columns) database used to test the 0003
-- upgrade. Mirrors the shape of the local development data: question IDs with gaps
-- (1, 4, 5), the renamed identities First Last and Rahul Sharma, plus awkward text.
-- Responses are inserted with triggers disabled so they can carry historical timestamps;
-- their scores are consistent with the answer key, as 0002 would produce.
INSERT INTO questions (question_id, question_date, question, option_a, option_b, option_c, option_d, correct_answer, created_at)
OVERRIDING SYSTEM VALUE VALUES
    (1, '2026-10-23', 'Demo: which city is the capital of India? (today)', 'Mumbai', 'New Delhi', 'Chennai', 'Kolkata', 'B',
     '2026-10-23 14:36:13.677159+05:30'),
    (4, '2026-10-24', 'Quote "test" and unicode: नमस्ते?', 'It''s A', 'B, with comma', E'C\nnewline', 'D', 'C',
     '2026-10-23 14:36:14.104557+05:30'),
    (5, '2026-10-22', 'No responses yet', '1', '2', '3', '4', 'D',
     '2026-10-23 14:36:14.156825+05:30');
SELECT setval('questions_question_id_seq', 5);

SET session_replication_role = replica;
INSERT INTO responses (response_id, question_id, user_id, user_name, response_answer, is_correct, responded_at)
OVERRIDING SYSTEM VALUE VALUES
    (1, 1, 'first.last@alrium.com',   'First Last',   'B', true,  '2026-10-23 14:36:13.7569+05:30'),
    (2, 1, 'rahul.sharma@alrium.com', 'Rahul Sharma', 'A', false, '2026-10-23 14:36:13.772368+05:30'),
    (3, 4, 'first.last@alrium.com',   'First Last',   'C', true,  '2026-10-24 00:00:00+05:30'),
    (7, 4, 'rahul.sharma@alrium.com', 'Rahul Sharma', 'D', false, '2026-10-24 23:59:59.999999+05:30');
SET session_replication_role = origin;
SELECT setval('responses_response_id_seq', 9);
