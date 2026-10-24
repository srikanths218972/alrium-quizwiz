-- 0002: move from UUID keys + JSONB question documents (0001) to integer identity
-- keys and separate TEXT columns. Also adds today-only submission (Asia/Kolkata).
--
-- Runs in one transaction (scripts/db migrate). Existing rows are copied, old UUIDs
-- are mapped to new integer IDs through the unique question_date, and every response
-- keeps its question, user, answer, score and timestamp. Any inconsistency raises an
-- error and the whole migration rolls back, leaving the 0001 tables untouched.
--
-- Text limits (kept in sync with the API and UI):
--   question 500, each option 200, user_id 320, user_name 200 characters.

LOCK TABLE questions, responses IN ACCESS EXCLUSIVE MODE;

-- ---------------------------------------------------------------------------
-- 1. Validate legacy data before copying.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    bad int;
BEGIN
    SELECT count(*) INTO bad FROM questions WHERE NOT qa_is_valid_question(question);
    IF bad > 0 THEN
        RAISE EXCEPTION '0002: % legacy question(s) fail JSON validation', bad;
    END IF;

    SELECT count(*) INTO bad
    FROM questions
    WHERE char_length(question ->> 'question') > 500
       OR char_length(question -> 'options' ->> 'A') > 200
       OR char_length(question -> 'options' ->> 'B') > 200
       OR char_length(question -> 'options' ->> 'C') > 200
       OR char_length(question -> 'options' ->> 'D') > 200;
    IF bad > 0 THEN
        RAISE EXCEPTION '0002: % legacy question(s) exceed text length limits', bad;
    END IF;

    SELECT count(*) INTO bad
    FROM responses AS r
    JOIN questions AS q USING (question_id)
    WHERE r.is_correct IS DISTINCT FROM (r.response_answer = q.question ->> 'answer');
    IF bad > 0 THEN
        RAISE EXCEPTION '0002: % legacy response(s) have a score that does not match the answer key', bad;
    END IF;

    SELECT count(*) INTO bad
    FROM responses
    WHERE char_length(user_id) > 320 OR char_length(user_name) > 200;
    IF bad > 0 THEN
        RAISE EXCEPTION '0002: % legacy response(s) exceed identity length limits', bad;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 2. New tables under temporary names (index-backed constraint names must be
--    unique, so they are renamed after the old tables are dropped).
-- ---------------------------------------------------------------------------
CREATE TABLE questions_new (
    question_id    int         GENERATED ALWAYS AS IDENTITY,
    question_date  date        NOT NULL,
    question       text        NOT NULL,
    option_a       text        NOT NULL,
    option_b       text        NOT NULL,
    option_c       text        NOT NULL,
    option_d       text        NOT NULL,
    correct_answer text        NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT questions_new_pkey PRIMARY KEY (question_id),
    CONSTRAINT questions_new_question_date_key UNIQUE (question_date),
    CONSTRAINT questions_question_text CHECK (question ~ '[^[:space:]]' AND char_length(question) <= 500),
    CONSTRAINT questions_option_a_text CHECK (option_a ~ '[^[:space:]]' AND char_length(option_a) <= 200),
    CONSTRAINT questions_option_b_text CHECK (option_b ~ '[^[:space:]]' AND char_length(option_b) <= 200),
    CONSTRAINT questions_option_c_text CHECK (option_c ~ '[^[:space:]]' AND char_length(option_c) <= 200),
    CONSTRAINT questions_option_d_text CHECK (option_d ~ '[^[:space:]]' AND char_length(option_d) <= 200),
    CONSTRAINT questions_correct_answer_check CHECK (correct_answer IN ('A', 'B', 'C', 'D'))
);

CREATE TABLE responses_new (
    response_id     int         GENERATED ALWAYS AS IDENTITY,
    question_id     int         NOT NULL,
    user_id         text        NOT NULL,
    user_name       text        NOT NULL,
    response_answer text        NOT NULL,
    is_correct      boolean     NOT NULL,
    responded_at    timestamptz NOT NULL,
    CONSTRAINT responses_new_pkey PRIMARY KEY (response_id),
    CONSTRAINT responses_new_question_id_fkey FOREIGN KEY (question_id)
        REFERENCES questions_new (question_id) ON DELETE RESTRICT,
    CONSTRAINT responses_new_question_id_user_id_key UNIQUE (question_id, user_id),
    CONSTRAINT responses_user_id_normalized CHECK (
        user_id = lower(user_id)
        AND user_id !~ '[[:space:]]'
        AND user_id ~ '^[^@]+@[^@]+$'
        AND char_length(user_id) <= 320
    ),
    CONSTRAINT responses_user_name_text CHECK (
        user_name ~ '[^[:space:]]'
        AND user_name !~ '^[[:space:]]|[[:space:]]$'
        AND char_length(user_name) <= 200
    ),
    CONSTRAINT responses_response_answer_check CHECK (response_answer IN ('A', 'B', 'C', 'D'))
);

-- ---------------------------------------------------------------------------
-- 3. Copy data. No triggers exist on the new tables yet, so legacy scores and
--    timestamps are preserved exactly.
-- ---------------------------------------------------------------------------
INSERT INTO questions_new (question_date, question, option_a, option_b, option_c, option_d, correct_answer, created_at)
SELECT question_date,
       question ->> 'question',
       question -> 'options' ->> 'A',
       question -> 'options' ->> 'B',
       question -> 'options' ->> 'C',
       question -> 'options' ->> 'D',
       question ->> 'answer',
       created_at
FROM questions
ORDER BY question_date;

CREATE TEMP TABLE qa_question_id_map ON COMMIT DROP AS
SELECT o.question_id AS old_id, n.question_id AS new_id
FROM questions AS o
JOIN questions_new AS n USING (question_date);

INSERT INTO responses_new (question_id, user_id, user_name, response_answer, is_correct, responded_at)
SELECT m.new_id, r.user_id, r.user_name, r.response_answer, r.is_correct, r.responded_at
FROM responses AS r
JOIN qa_question_id_map AS m ON m.old_id = r.question_id
ORDER BY r.responded_at, r.response_id;

-- ---------------------------------------------------------------------------
-- 4. Verify the copy: counts, mapping and every response field.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    diff int;
BEGIN
    IF (SELECT count(*) FROM questions) <> (SELECT count(*) FROM questions_new)
       OR (SELECT count(*) FROM questions) <> (SELECT count(*) FROM qa_question_id_map) THEN
        RAISE EXCEPTION '0002: question count or ID mapping mismatch';
    END IF;

    IF (SELECT count(*) FROM responses) <> (SELECT count(*) FROM responses_new) THEN
        RAISE EXCEPTION '0002: response count mismatch';
    END IF;

    SELECT count(*) INTO diff FROM (
        (SELECT q.question_date, r.user_id, r.user_name, r.response_answer, r.is_correct, r.responded_at
         FROM responses r JOIN questions q USING (question_id)
         EXCEPT
         SELECT q.question_date, r.user_id, r.user_name, r.response_answer, r.is_correct, r.responded_at
         FROM responses_new r JOIN questions_new q USING (question_id))
        UNION ALL
        (SELECT q.question_date, r.user_id, r.user_name, r.response_answer, r.is_correct, r.responded_at
         FROM responses_new r JOIN questions_new q USING (question_id)
         EXCEPT
         SELECT q.question_date, r.user_id, r.user_name, r.response_answer, r.is_correct, r.responded_at
         FROM responses r JOIN questions q USING (question_id))
    ) AS d;
    IF diff > 0 THEN
        RAISE EXCEPTION '0002: % response row(s) differ after copy', diff;
    END IF;

    SELECT count(*) INTO diff
    FROM responses_new r JOIN questions_new q USING (question_id)
    WHERE r.is_correct IS DISTINCT FROM (r.response_answer = q.correct_answer);
    IF diff > 0 THEN
        RAISE EXCEPTION '0002: % copied score(s) disagree with correct_answer', diff;
    END IF;

    SELECT count(*) INTO diff
    FROM questions o JOIN questions_new n USING (question_date)
    WHERE o.created_at IS DISTINCT FROM n.created_at;
    IF diff > 0 THEN
        RAISE EXCEPTION '0002: % question created_at value(s) changed', diff;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 5. Replace the old tables. Dropping them also drops their 0001 triggers; the
--    protections are recreated below for the new columns.
-- ---------------------------------------------------------------------------
DROP TABLE responses;
DROP TABLE questions;
DROP FUNCTION qa_responses_before_insert();
DROP FUNCTION qa_questions_before_update();
DROP FUNCTION qa_responses_block_mutation();
DROP FUNCTION qa_is_valid_question(jsonb);

ALTER TABLE questions_new RENAME TO questions;
ALTER TABLE questions RENAME CONSTRAINT questions_new_pkey TO questions_pkey;
ALTER TABLE questions RENAME CONSTRAINT questions_new_question_date_key TO questions_question_date_key;
ALTER SEQUENCE questions_new_question_id_seq RENAME TO questions_question_id_seq;

ALTER TABLE responses_new RENAME TO responses;
ALTER TABLE responses RENAME CONSTRAINT responses_new_pkey TO responses_pkey;
ALTER TABLE responses RENAME CONSTRAINT responses_new_question_id_fkey TO responses_question_id_fkey;
ALTER TABLE responses RENAME CONSTRAINT responses_new_question_id_user_id_key TO responses_question_id_user_id_key;
ALTER SEQUENCE responses_new_response_id_seq RENAME TO responses_response_id_seq;

COMMENT ON TABLE questions IS 'One quiz question per calendar date in Asia/Kolkata.';
COMMENT ON COLUMN questions.question_date IS 'Business date (Asia/Kolkata). UNIQUE: at most one question per date; completeness is not enforced.';
COMMENT ON COLUMN questions.correct_answer IS 'A, B, C or D. Never returned to a participant before they answer.';
COMMENT ON TABLE responses IS 'One answer per user per question. Append-only through the application path.';
COMMENT ON COLUMN responses.user_id IS 'Verified corporate email, trimmed and lowercased. Stored data, not authentication.';
COMMENT ON COLUMN responses.user_name IS 'Verified display-name snapshot at submission time (trimmed).';
COMMENT ON COLUMN responses.is_correct IS 'Computed by trigger from questions.correct_answer; caller values ignored.';
COMMENT ON COLUMN responses.responded_at IS 'Assigned by trigger from clock_timestamp() after the question lock; caller values ignored.';

-- ---------------------------------------------------------------------------
-- 6. Business date, scoring and history rules.
-- ---------------------------------------------------------------------------
CREATE FUNCTION qa_business_date(ts timestamptz)
RETURNS date
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$ SELECT (ts AT TIME ZONE 'Asia/Kolkata')::date $$;

COMMENT ON FUNCTION qa_business_date(timestamptz) IS 'Calendar date of an instant in the business timezone (Asia/Kolkata).';

-- Locking protocol: a submission locks the parent question FOR SHARE; question
-- UPDATE/DELETE take a conflicting row lock. After the lock is held, a single
-- clock_timestamp() reading is both the eligibility instant (is it today in
-- Asia/Kolkata?) and the stored responded_at, so a request that waited across
-- midnight is judged by the time it actually proceeds, not its transaction start.
CREATE FUNCTION qa_responses_before_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER  -- FOR SHARE needs UPDATE privilege; the app role should not hold it.
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    q_date   date;
    q_answer text;
    instant  timestamptz;
BEGIN
    NEW.user_id   := lower(regexp_replace(NEW.user_id, '^[[:space:]]+|[[:space:]]+$', '', 'g'));
    NEW.user_name := regexp_replace(NEW.user_name, '^[[:space:]]+|[[:space:]]+$', '', 'g');
    NEW.is_correct := NULL;
    NEW.responded_at := NULL;

    IF NEW.question_id IS NULL THEN
        RETURN NEW;  -- NOT NULL constraint reports it
    END IF;

    SELECT q.question_date, q.correct_answer INTO q_date, q_answer
    FROM public.questions AS q
    WHERE q.question_id = NEW.question_id
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'question % does not exist', NEW.question_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF NEW.response_answer IS NULL THEN
        RETURN NEW;  -- NOT NULL constraint reports it
    END IF;

    IF NEW.response_answer NOT IN ('A', 'B', 'C', 'D') THEN
        RAISE EXCEPTION 'response_answer must be one of A, B, C, D'
            USING ERRCODE = 'check_violation';
    END IF;

    instant := clock_timestamp();
    IF q_date <> public.qa_business_date(instant) THEN
        RAISE EXCEPTION 'question % is for %, not today (%) in Asia/Kolkata',
                        NEW.question_id, q_date, public.qa_business_date(instant)
            USING ERRCODE = 'QA001';
    END IF;

    NEW.responded_at := instant;
    NEW.is_correct := (NEW.response_answer = q_answer);
    RETURN NEW;
END;
$$;

CREATE TRIGGER responses_before_insert
    BEFORE INSERT ON responses
    FOR EACH ROW EXECUTE FUNCTION qa_responses_before_insert();

CREATE FUNCTION qa_responses_block_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'responses are append-only: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER responses_block_update_delete
    BEFORE UPDATE OR DELETE ON responses
    FOR EACH ROW EXECUTE FUNCTION qa_responses_block_mutation();

CREATE TRIGGER responses_block_truncate
    BEFORE TRUNCATE ON responses
    FOR EACH STATEMENT EXECUTE FUNCTION qa_responses_block_mutation();

CREATE FUNCTION qa_questions_before_update()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER  -- must see all responses regardless of the caller's grants
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF NEW.question_id IS DISTINCT FROM OLD.question_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'question_id and created_at cannot be changed'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF (NEW.question_date, NEW.question, NEW.option_a, NEW.option_b, NEW.option_c, NEW.option_d, NEW.correct_answer)
       IS DISTINCT FROM
       (OLD.question_date, OLD.question, OLD.option_a, OLD.option_b, OLD.option_c, OLD.option_d, OLD.correct_answer) THEN
        IF current_setting('transaction_isolation') = 'repeatable read' THEN
            RAISE EXCEPTION 'question edits must run at READ COMMITTED or SERIALIZABLE isolation'
                USING ERRCODE = 'object_not_in_prerequisite_state';
        END IF;

        -- Runs after this UPDATE holds the row lock, with a fresh snapshot.
        IF EXISTS (SELECT 1 FROM public.responses AS r WHERE r.question_id = OLD.question_id) THEN
            RAISE EXCEPTION 'question % already has responses and can no longer be changed', OLD.question_id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER questions_before_update
    BEFORE UPDATE ON questions
    FOR EACH ROW EXECUTE FUNCTION qa_questions_before_update();
