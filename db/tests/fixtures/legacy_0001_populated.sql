-- Populated 0001 (UUID + JSONB) database used to test the 0002 upgrade.
-- Responses are inserted with triggers disabled so they can carry varied historical
-- timestamps; their scores are consistent with the answer key, as 0001 would produce.
INSERT INTO questions (question_id, question_date, question, created_at) VALUES
    ('11111111-1111-1111-1111-111111111111', '2026-09-01',
     '{"question":"Which database are we using?","options":{"A":"PostgreSQL","B":"MySQL","C":"SQLite","D":"MongoDB"},"answer":"A"}',
     '2026-08-25 10:00:00+05:30'),
    ('22222222-2222-2222-2222-222222222222', '2026-09-02',
     '{"question":"Quote \"test\" and unicode: नमस्ते?","options":{"A":"It''s A","B":"B, with comma","C":"C\nnewline","D":"D"},"answer":"C"}',
     '2026-08-26 11:30:00+05:30'),
    ('33333333-3333-3333-3333-333333333333', '2026-09-03',
     '{"question":"No responses yet","options":{"A":"1","B":"2","C":"3","D":"4"},"answer":"D"}',
     '2026-08-27 09:15:00+05:30');

SET session_replication_role = replica;
INSERT INTO responses (response_id, question_id, user_id, user_name, response_answer, is_correct, responded_at) VALUES
    ('aaaaaaaa-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', 'asha@example.com', 'Asha Legacy', 'A', true,  '2026-09-01 09:01:02.123456+05:30'),
    ('aaaaaaaa-0000-0000-0000-000000000002', '11111111-1111-1111-1111-111111111111', 'ravi@example.com', 'Ravi Legacy', 'B', false, '2026-09-01 09:05:00+05:30'),
    ('aaaaaaaa-0000-0000-0000-000000000003', '22222222-2222-2222-2222-222222222222', 'asha@example.com', 'Asha Legacy', 'C', true,  '2026-09-02 23:59:59.999999+05:30'),
    ('aaaaaaaa-0000-0000-0000-000000000004', '22222222-2222-2222-2222-222222222222', 'meera@example.com', 'Meera Legacy', 'D', false, '2026-09-02 00:00:00+05:30');
SET session_replication_role = origin;
