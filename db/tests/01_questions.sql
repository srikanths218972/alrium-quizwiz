-- Question table shape. Payload validation is the API's job (the meeting removed the
-- database CHECK), so the table only enforces NOT NULL and identity.
SELECT qa_test.ok('schema: questions columns are question_id, question_date, question, created_at',
    (SELECT string_agg(column_name || ':' || data_type, ',' ORDER BY ordinal_position)
     FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'questions')
    = 'question_id:integer,question_date:date,question:jsonb,created_at:timestamp with time zone');
SELECT qa_test.ok('schema: question_id is int identity ALWAYS',
    (SELECT data_type = 'integer' AND is_identity = 'YES' AND identity_generation = 'ALWAYS'
     FROM information_schema.columns WHERE table_name = 'questions' AND column_name = 'question_id'));
SELECT qa_test.ok('schema: response_id is int identity ALWAYS',
    (SELECT data_type = 'integer' AND is_identity = 'YES' AND identity_generation = 'ALWAYS'
     FROM information_schema.columns WHERE table_name = 'responses' AND column_name = 'response_id'));
SELECT qa_test.ok('schema: no UNIQUE or CHECK constraint on questions (multiple per date, no payload CHECK)',
    NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'questions'::regclass AND contype IN ('u', 'c')));
SELECT qa_test.ok('schema: no unique index on questions other than the primary key',
    NOT EXISTS (SELECT 1 FROM pg_index WHERE indrelid = 'questions'::regclass AND indisunique AND NOT indisprimary));
SELECT qa_test.ok('schema: qa_is_valid_question function no longer exists',
    NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'qa_is_valid_question'));
SELECT qa_test.ok('schema: latest-question index on (question_date, created_at DESC, question_id DESC)',
    (SELECT pg_get_indexdef('questions_date_latest_idx'::regclass) LIKE '%(question_date, created_at DESC, question_id DESC)'));
SELECT qa_test.ok('schema: no uuid or json columns; only questions.question is jsonb',
    (SELECT string_agg(table_name || '.' || column_name, ',') FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name IN ('questions', 'responses') AND data_type IN ('json', 'jsonb', 'uuid'))
    = 'questions.question');

SELECT qa_test.throws('questions: NULL date rejected',
    $$INSERT INTO questions (question_date, question) VALUES (NULL, qa_test.payload())$$, '23502');
SELECT qa_test.throws('questions: NULL payload rejected',
    $$INSERT INTO questions (question_date, question) VALUES ('2030-01-01', NULL)$$, '23502');
SELECT qa_test.throws('questions: caller cannot supply question_id (identity ALWAYS)',
    $$INSERT INTO questions (question_id, question_date, question) VALUES (999, '2030-01-02', qa_test.payload())$$, '428C9');

INSERT INTO questions (question_date, question) VALUES ('2030-01-01', qa_test.payload('Alpha', 'Version 1'));
INSERT INTO questions (question_date, question) VALUES ('2030-01-01', qa_test.payload('Bravo', 'Version 2'));
SELECT qa_test.ok('questions: several questions may share a date',
    (SELECT count(*) = 2 FROM questions WHERE question_date = '2030-01-01'));
SELECT qa_test.ok('questions: integer identity and created_at assigned',
    (SELECT bool_and(question_id > 0 AND created_at IS NOT NULL) FROM questions WHERE question_date = '2030-01-01'));

-- Latest selection, including an exact created_at tie broken by question_id.
SET session_replication_role = replica;  -- allow back-dated created_at for the test rows only
INSERT INTO questions (question_date, question, created_at) VALUES
    ('2030-02-01', qa_test.payload('Alpha', 'Old'),   '2030-01-01 09:00+05:30'),
    ('2030-02-01', qa_test.payload('Alpha', 'Tie 1'), '2030-01-01 10:00+05:30'),
    ('2030-02-01', qa_test.payload('Alpha', 'Tie 2'), '2030-01-01 10:00+05:30'),
    ('2030-02-02', qa_test.payload('Alpha', 'Other day'), '2030-01-01 11:00+05:30');
SET session_replication_role = origin;
SELECT qa_test.ok('latest: newest created_at wins and equal timestamps fall back to the higher question_id',
    (SELECT question ->> 'question' FROM questions WHERE question_id = qa_test.latest('2030-02-01')) = 'Tie 2');
SELECT qa_test.ok('latest: rows for other dates are ignored',
    (SELECT question ->> 'question' FROM questions WHERE question_id = qa_test.latest('2030-02-02')) = 'Other day');

-- Business date boundaries (Asia/Kolkata is UTC+05:30).
SELECT qa_test.ok('business date: 18:29:59 UTC is still the same Delhi day',
    qa_business_date('2026-10-23 18:29:59+00') = '2026-10-23');
SELECT qa_test.ok('business date: 18:30:00 UTC is the next Delhi day',
    qa_business_date('2026-10-23 18:30:00+00') = '2026-10-24');
SELECT qa_test.ok('business date: 23:59:59.999999 Delhi is still that day',
    qa_business_date('2026-10-23 23:59:59.999999+05:30') = '2026-10-23');
SELECT qa_test.ok('business date: independent of session TimeZone',
    (SELECT set_config('TimeZone', 'America/Los_Angeles', true) IS NOT NULL
        AND qa_business_date('2026-10-23 23:59:00+05:30') = '2026-10-23'));
