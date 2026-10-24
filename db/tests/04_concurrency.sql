-- Concurrency tests using two independent sessions (dblink connections a and b).
-- Ordering is forced: session a holds its lock, b is sent asynchronously, the test
-- waits until b is actually blocked on a, then a commits or rolls back.
-- Requires psql variable :connstr. Setup statements autocommit so both sessions see them.
SELECT dblink_connect('a', :'connstr'), dblink_connect('b', :'connstr');

CREATE FUNCTION pg_temp.qid() RETURNS int LANGUAGE sql AS
    $$ SELECT qa_test.latest(qa_test.today()) $$;
CREATE FUNCTION pg_temp.ins_for(id int, uid text, ans text) RETURNS text LANGUAGE sql AS
    $$ SELECT format('INSERT INTO responses (question_id, user_id, user_name, response_answer) VALUES (%s, %L, %L, %L)',
                     id, uid, 'Concurrent User', ans) $$;
CREATE FUNCTION pg_temp.ins(uid text, ans text) RETURNS text LANGUAGE sql AS
    $$ SELECT pg_temp.ins_for(pg_temp.qid(), uid, ans) $$;
CREATE FUNCTION pg_temp.b_pid() RETURNS int LANGUAGE sql AS
    $$ SELECT pid FROM dblink('b', 'SELECT pg_backend_pid()') AS t(pid int) $$;

-- 1. Two submissions by the same (normalized) user; second blocks on the first's unique entry.
SELECT qa_test.fresh_question(qa_test.today(), 'Alpha', 'Simultaneous duplicate');
DO $$
DECLARE b_pid int := pg_temp.b_pid(); st text;
BEGIN
    PERFORM dblink_exec('a', 'BEGIN');
    PERFORM dblink_exec('a', pg_temp.ins('race@alrium.com', 'Alpha'));
    PERFORM dblink_send_query('b', pg_temp.ins(' RACE@alrium.com', 'Bravo'));
    PERFORM qa_test.ok('concurrent duplicate: second submission blocked by first', qa_test.wait_blocked(b_pid));
    PERFORM dblink_exec('a', 'COMMIT');
    st := qa_test.async_state('b');
    PERFORM qa_test.ok('concurrent duplicate: second submission rejected 23505', st = '23505', st);
    PERFORM qa_test.ok('concurrent duplicate: exactly one row stored, with the first answer',
        (SELECT count(*) = 1 AND bool_and(response_answer = 'Alpha') FROM responses
         WHERE question_id = pg_temp.qid() AND user_id = 'race@alrium.com'));
END;
$$;

-- 2. Same, but the first transaction rolls back: the second then succeeds.
SELECT qa_test.fresh_question(qa_test.today(), 'Alpha', 'Duplicate after rollback');
DO $$
DECLARE b_pid int := pg_temp.b_pid(); st text;
BEGIN
    PERFORM dblink_exec('a', 'BEGIN');
    PERFORM dblink_exec('a', pg_temp.ins('race@alrium.com', 'Alpha'));
    PERFORM dblink_send_query('b', pg_temp.ins('race@alrium.com', 'Bravo'));
    PERFORM qa_test.ok('duplicate after rollback: second submission blocked', qa_test.wait_blocked(b_pid));
    PERFORM dblink_exec('a', 'ROLLBACK');
    st := qa_test.async_state('b');
    PERFORM qa_test.ok('duplicate after rollback: second submission stored',
        st IS NULL AND (SELECT response_answer = 'Bravo' FROM responses WHERE question_id = pg_temp.qid()), st);
END;
$$;

-- 3. A newer version inserted while a submission for the older ID is in flight: neither
--    blocks the other, and the submission is scored against the ID it named.
SELECT qa_test.fresh_question(qa_test.today(), 'Alpha', 'In-flight version 1') AS v1 \gset
DO $$
DECLARE st text; v1 int := pg_temp.qid();
BEGIN
    PERFORM dblink_exec('a', 'BEGIN');
    PERFORM dblink_exec('a', pg_temp.ins_for(v1, 'flight@alrium.com', 'Alpha'));
    PERFORM dblink_send_query('b', format('INSERT INTO questions (question_date, question) VALUES (%L, %L)',
                                          qa_test.today(), qa_test.payload('Bravo', 'In-flight version 2')));
    st := qa_test.async_state('b');
    PERFORM qa_test.ok('in-flight: inserting a newer version is not blocked by a submission', st IS NULL, st);
    PERFORM dblink_exec('a', 'COMMIT');
    PERFORM qa_test.ok('in-flight: submission kept its question_id and score',
        (SELECT question_id = v1 AND is_correct AND question = 'In-flight version 1'
         FROM responses WHERE user_id = 'flight@alrium.com'));
    PERFORM qa_test.ok('in-flight: the newer version is now served', pg_temp.qid() <> v1);
END;
$$;

-- 4. Unforced race: both duplicate submissions sent at once in autocommit mode.
SELECT qa_test.fresh_question(qa_test.today(), 'Alpha', 'Unforced race');
DO $$
DECLARE sa text; sb text;
BEGIN
    PERFORM dblink_send_query('a', pg_temp.ins('both@alrium.com', 'Alpha'));
    PERFORM dblink_send_query('b', pg_temp.ins('Both@Alrium.com', 'Bravo'));
    sa := qa_test.async_state('a');
    sb := qa_test.async_state('b');
    PERFORM qa_test.ok('unforced race: exactly one succeeds, other gets 23505',
        (sa IS NULL AND sb = '23505') OR (sb IS NULL AND sa = '23505'), format('a=%s b=%s', sa, sb));
    PERFORM qa_test.ok('unforced race: one row stored',
        (SELECT count(*) = 1 FROM responses WHERE question_id = pg_temp.qid()));
END;
$$;

-- 5. Eligibility instant is read after the lock, not at transaction start. Session b
--    starts its transaction, then its submission waits about 1.5 s behind a row lock held
--    by session a. The stored responded_at must be at least 1.5 s after b's now(). This is
--    what makes a request that waits across midnight judged by when it proceeds.
SELECT qa_test.fresh_question(qa_test.today(), 'Alpha', 'Timestamp after lock wait');
DO $$
DECLARE b_pid int := pg_temp.b_pid(); st text; waited interval;
BEGIN
    PERFORM dblink_exec('a', 'BEGIN');
    PERFORM * FROM dblink('a', format('SELECT 1 FROM questions WHERE question_id = %s FOR UPDATE', pg_temp.qid())) AS t(x int);
    PERFORM dblink_exec('b', 'BEGIN');
    PERFORM * FROM dblink('b', 'SELECT now()') AS t(x timestamptz);
    PERFORM dblink_send_query('b', pg_temp.ins('waiter@alrium.com', 'Alpha'));
    PERFORM qa_test.ok('lock wait: submission blocked', qa_test.wait_blocked(b_pid));
    PERFORM pg_sleep(1.5);
    PERFORM dblink_exec('a', 'COMMIT');
    st := qa_test.async_state('b');
    SELECT w INTO waited FROM dblink('b',
        'SELECT responded_at - now() FROM responses WHERE user_id = ''waiter@alrium.com''') AS t(w interval);
    PERFORM dblink_exec('b', 'COMMIT');
    PERFORM qa_test.ok('lock wait: responded_at taken after the lock (>= 1.5 s after transaction start)',
        st IS NULL AND waited >= interval '1.5 seconds', format('state=%s waited=%s', st, waited));
END;
$$;

SELECT dblink_disconnect('a'), dblink_disconnect('b');
