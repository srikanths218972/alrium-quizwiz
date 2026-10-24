-- Test helpers. Only ever loaded into the disposable test project's database.
CREATE EXTENSION IF NOT EXISTS dblink;
CREATE SCHEMA IF NOT EXISTS qa_test;
CREATE TABLE IF NOT EXISTS qa_test.results (
    id     serial PRIMARY KEY,
    name   text    NOT NULL,
    ok     boolean NOT NULL,
    detail text
);

CREATE OR REPLACE FUNCTION qa_test.ok(test_name text, cond boolean, detail text DEFAULT NULL)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO qa_test.results (name, ok, detail) VALUES (test_name, coalesce(cond, false), detail);
$$;

-- Runs stmt in a subtransaction (rolled back) and records whether it raised expected_state.
CREATE OR REPLACE FUNCTION qa_test.throws(test_name text, stmt text, expected_state text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    BEGIN
        EXECUTE stmt;
    EXCEPTION WHEN OTHERS THEN
        PERFORM qa_test.ok(test_name, SQLSTATE = expected_state,
                           format('expected %s, got %s: %s', expected_state, SQLSTATE, SQLERRM));
        RETURN;
    END;
    PERFORM qa_test.ok(test_name, false, format('expected %s, no error raised', expected_state));
END;
$$;

-- Waits until backend pid is blocked by a lock (up to about 10 seconds).
CREATE OR REPLACE FUNCTION qa_test.wait_blocked(pid int)
RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
    FOR i IN 1..200 LOOP
        IF cardinality(pg_blocking_pids(pid)) > 0 THEN
            RETURN true;
        END IF;
        PERFORM pg_sleep(0.05);
    END LOOP;
    RETURN false;
END;
$$;

-- Collects the async result on conn; returns NULL on success or the SQLSTATE raised.
CREATE OR REPLACE FUNCTION qa_test.async_state(conn text)
RETURNS text LANGUAGE plpgsql AS $$
BEGIN
    PERFORM * FROM dblink_get_result(conn) AS t(x text);
    PERFORM * FROM dblink_get_result(conn) AS t(x text);  -- drain the terminating empty result
    RETURN NULL;
EXCEPTION WHEN OTHERS THEN
    PERFORM * FROM dblink_get_result(conn, false) AS t(x text);
    RETURN SQLSTATE;
END;
$$;

CREATE OR REPLACE FUNCTION qa_test.today() RETURNS date LANGUAGE sql STABLE AS
    $$ SELECT qa_business_date(clock_timestamp()) $$;

-- Test-only cleanup: removes every question on date d and their responses by
-- disabling triggers for this session (superuser only, disposable database).
CREATE OR REPLACE FUNCTION qa_test.clear_date(d date) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    SET LOCAL session_replication_role = replica;
    DELETE FROM responses WHERE question_id IN (SELECT question_id FROM questions WHERE question_date = d);
    DELETE FROM questions WHERE question_date = d;
    SET LOCAL session_replication_role = origin;
END;
$$;

-- JSONB payload with options Alpha, Bravo, Charlie, Delta; answer is the actual option text.
CREATE OR REPLACE FUNCTION qa_test.payload(answer text DEFAULT 'Alpha', prompt text DEFAULT 'Test question')
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_build_object('question', prompt,
        'options', jsonb_build_object('A', 'Alpha', 'B', 'Bravo', 'C', 'Charlie', 'D', 'Delta'), 'answer', answer)
$$;

-- Creates the only question on date d (clearing any previous ones) and returns its ID.
CREATE OR REPLACE FUNCTION qa_test.fresh_question(d date, answer text DEFAULT 'Alpha', prompt text DEFAULT 'Test question')
RETURNS int LANGUAGE plpgsql AS $$
DECLARE
    id int;
BEGIN
    PERFORM qa_test.clear_date(d);
    INSERT INTO questions (question_date, question) VALUES (d, qa_test.payload(answer, prompt))
    RETURNING question_id INTO id;
    RETURN id;
END;
$$;

-- Latest question for date d, as served to participants.
CREATE OR REPLACE FUNCTION qa_test.latest(d date) RETURNS int LANGUAGE sql STABLE AS $$
    SELECT question_id FROM questions WHERE question_date = d ORDER BY created_at DESC, question_id DESC LIMIT 1
$$;
