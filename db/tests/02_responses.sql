-- Submission: option-text matching, copied text, scoring, today-only rule,
-- fail-closed payload checks, normalization and timestamps.
SELECT qa_test.fresh_question(qa_test.today(), 'Bravo', 'Scoring') AS today_qid \gset

SELECT qa_test.ok('schema: responses columns in ERD order',
    (SELECT string_agg(column_name || ':' || data_type, ',' ORDER BY ordinal_position)
     FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'responses')
    = 'response_id:integer,question_id:integer,user_id:text,user_name:text,question:text,correct_answer:text,'
      'response_answer:text,is_correct:boolean,responded_at:timestamp with time zone');
SELECT qa_test.ok('schema: no letter-based CHECK on responses.response_answer',
    NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'responses'::regclass AND contype = 'c'
                AND pg_get_constraintdef(oid) LIKE '%response_answer%'));
SELECT qa_test.ok('schema: UNIQUE (question_id, user_id) exists once',
    (SELECT count(*) = 1 FROM pg_constraint WHERE conrelid = 'responses'::regclass AND contype = 'u'
     AND pg_get_constraintdef(oid) = 'UNIQUE (question_id, user_id)'));
SELECT qa_test.ok('schema: FK to questions is ON DELETE RESTRICT',
    (SELECT confdeltype = 'r' FROM pg_constraint WHERE conname = 'responses_question_id_fkey'));

DO $$
DECLARE
    qid int := qa_test.latest(qa_test.today());
    r responses;
BEGIN
    INSERT INTO responses (question_id, user_id, user_name, question, correct_answer, response_answer, is_correct, responded_at)
    VALUES (qid, 'right@alrium.com', 'Right User', 'Forged question', 'Alpha', 'Bravo', false, '2000-01-01')
    RETURNING * INTO r;
    PERFORM qa_test.ok('submit: correct text scored true even when caller says false', r.is_correct IS TRUE);
    PERFORM qa_test.ok('submit: question text copied from the payload, caller value ignored', r.question = 'Scoring', r.question);
    PERFORM qa_test.ok('submit: correct_answer is the option text, caller value ignored', r.correct_answer = 'Bravo', r.correct_answer);
    PERFORM qa_test.ok('submit: response_answer stored as option text', r.response_answer = 'Bravo', r.response_answer);
    PERFORM qa_test.ok('submit: caller responded_at overwritten', r.responded_at > '2001-01-01');
    PERFORM qa_test.ok('submit: responded_at from database clock',
        r.responded_at >= now() AND r.responded_at <= clock_timestamp(),
        format('responded_at=%s, txn start=%s', r.responded_at, now()));

    INSERT INTO responses (question_id, user_id, user_name, response_answer, is_correct, responded_at)
    VALUES (qid, 'wrong@alrium.com', 'Wrong User', 'Charlie', true, '2999-01-01')
    RETURNING * INTO r;
    PERFORM qa_test.ok('submit: incorrect text scored false even when caller says true', r.is_correct IS FALSE);
    PERFORM qa_test.ok('submit: incorrect response keeps both texts', r.response_answer = 'Charlie' AND r.correct_answer = 'Bravo');

    INSERT INTO responses (question_id, user_id, user_name, response_answer)
    VALUES (qid, '  Mixed.Case@Alrium.COM ', '  Mixed Case  ', '  Alpha ')
    RETURNING * INTO r;
    PERFORM qa_test.ok('submit: surrounding spaces matched; canonical option text stored', r.response_answer = 'Alpha', r.response_answer);
    PERFORM qa_test.ok('submit: user_id trimmed and lowercased', r.user_id = 'mixed.case@alrium.com', r.user_id);
    PERFORM qa_test.ok('submit: user_name trimmed', r.user_name = 'Mixed Case', r.user_name);
END;
$$;

SELECT qa_test.throws('submit: caller cannot supply response_id (identity ALWAYS)',
    format($$INSERT INTO responses (response_id, question_id, user_id, user_name, response_answer)
             VALUES (5, %s, 'id@alrium.com', 'Id', 'Alpha')$$, :today_qid), '428C9');
SELECT qa_test.throws('submit: normalized email duplicate rejected',
    format($$INSERT INTO responses (question_id, user_id, user_name, response_answer)
             VALUES (%s, 'MIXED.case@alrium.com', 'Again', 'Bravo')$$, :today_qid), '23505');
SELECT qa_test.throws('submit: exact duplicate rejected',
    format($$INSERT INTO responses (question_id, user_id, user_name, response_answer)
             VALUES (%s, 'right@alrium.com', 'Right User', 'Bravo')$$, :today_qid), '23505');

DO $$
DECLARE
    qid int := qa_test.latest(qa_test.today());
    c record;
BEGIN
    FOR c IN SELECT * FROM (VALUES
        ('answer letter B',     'u1@alrium.com', 'U', 'B',       'QA003'),
        ('answer wrong case',   'u1@alrium.com', 'U', 'bravo',   'QA003'),
        ('answer not an option','u1@alrium.com', 'U', 'Echo',    'QA003'),
        ('answer empty',        'u1@alrium.com', 'U', '',        'QA003'),
        ('answer NULL',         'u1@alrium.com', 'U', NULL,      '23502'),
        ('user_id empty',       '',              'U', 'Alpha',   '23514'),
        ('user_id blank',       '   ',           'U', 'Alpha',   '23514'),
        ('user_id NULL',        NULL,            'U', 'Alpha',   '23502'),
        ('user_id not email',   'just-a-name',   'U', 'Alpha',   '23514'),
        ('user_id inner space', 'a b@alrium.com','U', 'Alpha',   '23514'),
        ('user_id over 320',    repeat('u', 310) || '@alrium.com', 'U', 'Alpha', '23514'),
        ('user_name empty',     'u1@alrium.com', '',  'Alpha',   '23514'),
        ('user_name blank',     'u1@alrium.com', '  ','Alpha',   '23514'),
        ('user_name NULL',      'u1@alrium.com', NULL,'Alpha',   '23502'),
        ('user_name over 200',  'u1@alrium.com', repeat('n', 201), 'Alpha', '23514')
    ) AS t (label, uid, uname, ans, state)
    LOOP
        PERFORM qa_test.throws('submit rejects: ' || c.label,
            format('INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (%s, %L, %L, %L)',
                   qid, c.uid, c.uname, c.ans), c.state);
    END LOOP;
END;
$$;

SELECT qa_test.throws('submit: unknown question_id rejected (FK)',
    $$INSERT INTO responses (question_id, user_id, user_name, response_answer)
      VALUES (2147483000, 'u2@alrium.com', 'U', 'Alpha')$$, '23503');
SELECT qa_test.throws('submit: NULL question_id rejected',
    $$INSERT INTO responses (question_id, user_id, user_name, response_answer)
      VALUES (NULL, 'u2@alrium.com', 'U', 'Alpha')$$, '23502');

-- Fail closed: stored payloads the API would never accept cannot be scored.
DO $$
DECLARE
    c record;
    qid int;
BEGIN
    FOR c IN SELECT * FROM (VALUES
        ('duplicate options',  '{"question":"q","options":{"A":"x","B":"x","C":"y","D":"z"},"answer":"y"}'),
        ('duplicate after trim','{"question":"q","options":{"A":"x","B":" x ","C":"y","D":"z"},"answer":"y"}'),
        ('missing answer',     '{"question":"q","options":{"A":"a","B":"b","C":"c","D":"d"}}'),
        ('answer is a label',  '{"question":"q","options":{"A":"a","B":"b","C":"c","D":"d"},"answer":"A"}'),
        ('answer not an option','{"question":"q","options":{"A":"a","B":"b","C":"c","D":"d"},"answer":"e"}'),
        ('answer not a string','{"question":"q","options":{"A":"a","B":"b","C":"c","D":"d"},"answer":1}'),
        ('missing option D',   '{"question":"q","options":{"A":"a","B":"b","C":"c"},"answer":"a"}'),
        ('extra option E',     '{"question":"q","options":{"A":"a","B":"b","C":"c","D":"d","E":"e"},"answer":"a"}'),
        ('numeric option',     '{"question":"q","options":{"A":1,"B":"b","C":"c","D":"d"},"answer":"b"}'),
        ('blank option',       '{"question":"q","options":{"A":" ","B":"b","C":"c","D":"d"},"answer":"b"}'),
        ('blank question',     '{"question":"  ","options":{"A":"a","B":"b","C":"c","D":"d"},"answer":"a"}'),
        ('options array',      '{"question":"q","options":["a","b","c","d"],"answer":"a"}'),
        ('payload is a string','"just text"')
    ) AS t (label, raw)
    LOOP
        INSERT INTO questions (question_date, question) VALUES (qa_test.today(), c.raw::jsonb) RETURNING question_id INTO qid;
        PERFORM qa_test.throws('fail closed: ' || c.label,
            format($q$INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (%s, 'fc@alrium.com', 'U', 'a')$q$, qid),
            'QA002');
    END LOOP;
END;
$$;

SELECT qa_test.fresh_question(qa_test.today() - 1, 'Alpha', 'Yesterday') AS past_qid \gset
SELECT qa_test.fresh_question(qa_test.today() + 1, 'Alpha', 'Tomorrow') AS future_qid \gset
SELECT qa_test.throws('today-only: past question rejected (QA001)',
    format($$INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (%s, 'u3@alrium.com', 'U', 'Alpha')$$, :past_qid), 'QA001');
SELECT qa_test.throws('today-only: future question rejected (QA001)',
    format($$INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (%s, 'u3@alrium.com', 'U', 'Alpha')$$, :future_qid), 'QA001');
SELECT qa_test.ok('today-only: rule does not depend on session TimeZone',
    (SELECT set_config('TimeZone', 'Pacific/Kiritimati', false) IS NOT NULL));
SELECT qa_test.throws('today-only: tomorrow still closed with session TimeZone UTC+14',
    format($$INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (%s, 'u4@alrium.com', 'U', 'Alpha')$$, :future_qid), 'QA001');
RESET TimeZone;

-- A single-letter option is an actual value: "B" is accepted when an option's text is "B".
DO $$
DECLARE qid int; r responses;
BEGIN
    INSERT INTO questions (question_date, question)
    VALUES (qa_test.today(), '{"question":"Second letter?","options":{"A":"A","B":"B","C":"C","D":"D"},"answer":"B"}')
    RETURNING question_id INTO qid;
    INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (qid, 'letter@alrium.com', 'L', 'B') RETURNING * INTO r;
    PERFORM qa_test.ok('letter values: "B" scored correct when "B" is the actual answer text',
        r.is_correct AND r.correct_answer = 'B' AND r.response_answer = 'B');
END;
$$;

-- Replacement policy: an older version for today stays answerable by its exact ID,
-- and one response per question (not per day) allows answering the replacement too.
SELECT qa_test.fresh_question(qa_test.today(), 'Alpha', 'Version 1') AS v1 \gset
INSERT INTO questions (question_date, question) VALUES (qa_test.today(), qa_test.payload('Delta', 'Version 2'));
SELECT qa_test.latest(qa_test.today()) AS v2 \gset
DO $$
DECLARE
    v1 int := (SELECT min(question_id) FROM questions WHERE question_date = qa_test.today());
    v2 int := qa_test.latest(qa_test.today());
    r responses;
BEGIN
    INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (v1, 'rep@alrium.com', 'Rep', 'Alpha') RETURNING * INTO r;
    PERFORM qa_test.ok('replacement: older version scored against its own answer', r.question_id = v1 AND r.is_correct AND r.question = 'Version 1');
    INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (v2, 'rep@alrium.com', 'Rep', 'Alpha') RETURNING * INTO r;
    PERFORM qa_test.ok('replacement: same user may answer the newer version, scored against it',
        r.question_id = v2 AND NOT r.is_correct AND r.correct_answer = 'Delta');
END;
$$;
