-- 0003: latest meeting decisions (2026-10-23).
--   * questions.question becomes one JSONB payload {"question","options":{"A".."D"},"answer"}
--     replacing the separate TEXT option and correct_answer columns. "answer" holds the
--     actual text of the correct option (e.g. "New Delhi"), never an option key.
--   * Several questions may share a date (no UNIQUE). The newest row for today is served.
--   * Questions are insert-only: UPDATE, DELETE and TRUNCATE are always refused.
--   * responses store actual text: question, correct_answer and response_answer
--     (the selected option's value, not its letter).
--
-- Runs in one transaction (scripts/db migrate). Tables are rebuilt so the column order
-- matches docs/latest-meeting-erd.mmd. Every question_id and response_id is preserved
-- with OVERRIDING SYSTEM VALUE, and identity sequences continue from their old position.
-- The 0002 correct_answer column is constrained to A/B/C/D, so it is unambiguously an
-- option key; it is translated to that option's text through the same row's columns.
-- Legacy rows are validated first; any inconsistency raises an error and the whole
-- migration rolls back, leaving the 0002 tables untouched.
--
-- Payload validation moved to the API (the meeting removed the database CHECK). The
-- response trigger still reads the payload defensively and refuses to score a malformed one.

LOCK TABLE questions, responses IN ACCESS EXCLUSIVE MODE;

-- ---------------------------------------------------------------------------
-- 1. Validate legacy data before transforming it.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    bad int;
BEGIN
    SELECT count(*) INTO bad FROM questions
    WHERE correct_answer IS NULL OR correct_answer NOT IN ('A', 'B', 'C', 'D')
       OR question !~ '[^[:space:]]' OR option_a !~ '[^[:space:]]' OR option_b !~ '[^[:space:]]'
       OR option_c !~ '[^[:space:]]' OR option_d !~ '[^[:space:]]';
    IF bad > 0 THEN
        RAISE EXCEPTION '0003: % legacy question(s) have blank text or an invalid answer key', bad;
    END IF;

    -- Responses will store option text, so options must be distinguishable.
    SELECT count(*) INTO bad FROM questions
    WHERE (SELECT count(DISTINCT btrim(v)) FROM unnest(ARRAY[option_a, option_b, option_c, option_d]) AS v) <> 4;
    IF bad > 0 THEN
        RAISE EXCEPTION '0003: % legacy question(s) have duplicate option text; letters cannot be translated unambiguously', bad;
    END IF;

    SELECT count(*) INTO bad FROM responses
    WHERE response_answer IS NULL OR response_answer NOT IN ('A', 'B', 'C', 'D');
    IF bad > 0 THEN
        RAISE EXCEPTION '0003: % legacy response(s) have an answer that is not A, B, C or D', bad;
    END IF;

    SELECT count(*) INTO bad FROM responses AS r
    WHERE NOT EXISTS (SELECT 1 FROM questions AS q WHERE q.question_id = r.question_id);
    IF bad > 0 THEN
        RAISE EXCEPTION '0003: % orphaned response(s) reference a missing question', bad;
    END IF;

    SELECT count(*) INTO bad
    FROM responses AS r JOIN questions AS q USING (question_id)
    WHERE r.is_correct IS DISTINCT FROM (r.response_answer = q.correct_answer);
    IF bad > 0 THEN
        RAISE EXCEPTION '0003: % legacy response(s) have a score that does not match the answer key', bad;
    END IF;
END;
$$;

-- Old sequence positions, so the next generated IDs are unchanged by the rebuild.
CREATE TEMP TABLE qa_0003_seq ON COMMIT DROP AS
SELECT 'questions' AS t, last_value, is_called FROM questions_question_id_seq
UNION ALL
SELECT 'responses', last_value, is_called FROM responses_response_id_seq;

-- Expected response rows after translation, built from the exact question_id.
CREATE TEMP TABLE qa_0003_expected ON COMMIT DROP AS
SELECT r.response_id, r.question_id, r.user_id, r.user_name,
       q.question AS question_text,
       CASE q.correct_answer WHEN 'A' THEN q.option_a WHEN 'B' THEN q.option_b
                             WHEN 'C' THEN q.option_c WHEN 'D' THEN q.option_d END AS correct_text,
       CASE r.response_answer WHEN 'A' THEN q.option_a WHEN 'B' THEN q.option_b
                              WHEN 'C' THEN q.option_c WHEN 'D' THEN q.option_d END AS answer_text,
       r.is_correct, r.responded_at
FROM responses AS r JOIN questions AS q USING (question_id);

-- ---------------------------------------------------------------------------
-- 2. New tables under temporary names.
-- ---------------------------------------------------------------------------
CREATE TABLE questions_new (
    question_id   int         GENERATED ALWAYS AS IDENTITY,
    question_date date        NOT NULL,
    question      jsonb       NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT questions_new_pkey PRIMARY KEY (question_id)
);

CREATE TABLE responses_new (
    response_id     int         GENERATED ALWAYS AS IDENTITY,
    question_id     int         NOT NULL,
    user_id         text        NOT NULL,
    user_name       text        NOT NULL,
    question        text        NOT NULL,
    correct_answer  text        NOT NULL,
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
    )
);

-- ---------------------------------------------------------------------------
-- 3. Copy. No triggers exist on the new tables yet, so legacy scores and
--    timestamps are preserved exactly.
-- ---------------------------------------------------------------------------
INSERT INTO questions_new (question_id, question_date, question, created_at)
OVERRIDING SYSTEM VALUE
SELECT question_id,
       question_date,
       jsonb_build_object(
           'question', question,
           'options', jsonb_build_object('A', option_a, 'B', option_b, 'C', option_c, 'D', option_d),
           'answer', CASE correct_answer WHEN 'A' THEN option_a WHEN 'B' THEN option_b
                                         WHEN 'C' THEN option_c WHEN 'D' THEN option_d END),
       created_at
FROM questions
ORDER BY question_id;

INSERT INTO responses_new (response_id, question_id, user_id, user_name, question, correct_answer,
                           response_answer, is_correct, responded_at)
OVERRIDING SYSTEM VALUE
SELECT response_id, question_id, user_id, user_name, question_text, correct_text,
       answer_text, is_correct, responded_at
FROM qa_0003_expected
ORDER BY response_id;

-- ---------------------------------------------------------------------------
-- 4. Verify the copy field by field.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    diff int;
BEGIN
    IF (SELECT count(*) FROM questions) <> (SELECT count(*) FROM questions_new)
       OR (SELECT count(*) FROM responses) <> (SELECT count(*) FROM responses_new) THEN
        RAISE EXCEPTION '0003: row count mismatch after copy';
    END IF;

    SELECT count(*) INTO diff
    FROM questions AS o FULL JOIN questions_new AS n USING (question_id)
    WHERE o.question_id IS NULL OR n.question_id IS NULL
       OR o.question_date IS DISTINCT FROM n.question_date
       OR o.created_at IS DISTINCT FROM n.created_at
       OR o.question IS DISTINCT FROM n.question ->> 'question'
       OR o.option_a IS DISTINCT FROM n.question -> 'options' ->> 'A'
       OR o.option_b IS DISTINCT FROM n.question -> 'options' ->> 'B'
       OR o.option_c IS DISTINCT FROM n.question -> 'options' ->> 'C'
       OR o.option_d IS DISTINCT FROM n.question -> 'options' ->> 'D'
       OR (CASE o.correct_answer WHEN 'A' THEN o.option_a WHEN 'B' THEN o.option_b
                                 WHEN 'C' THEN o.option_c WHEN 'D' THEN o.option_d END) IS DISTINCT FROM n.question ->> 'answer';
    IF diff > 0 THEN
        RAISE EXCEPTION '0003: % question(s) differ after conversion to JSONB', diff;
    END IF;

    SELECT count(*) INTO diff
    FROM responses AS o
    JOIN questions AS q USING (question_id)
    FULL JOIN responses_new AS n USING (response_id)
    WHERE o.response_id IS NULL OR n.response_id IS NULL
       OR o.question_id IS DISTINCT FROM n.question_id
       OR o.user_id IS DISTINCT FROM n.user_id
       OR o.user_name IS DISTINCT FROM n.user_name
       OR o.is_correct IS DISTINCT FROM n.is_correct
       OR o.responded_at IS DISTINCT FROM n.responded_at
       OR n.question IS DISTINCT FROM q.question
       OR n.response_answer IS DISTINCT FROM CASE o.response_answer WHEN 'A' THEN q.option_a WHEN 'B' THEN q.option_b
                                                                    WHEN 'C' THEN q.option_c WHEN 'D' THEN q.option_d END
       OR n.correct_answer IS DISTINCT FROM CASE q.correct_answer WHEN 'A' THEN q.option_a WHEN 'B' THEN q.option_b
                                                                  WHEN 'C' THEN q.option_c WHEN 'D' THEN q.option_d END;
    IF diff > 0 THEN
        RAISE EXCEPTION '0003: % response(s) differ after translation to text', diff;
    END IF;

    SELECT count(*) INTO diff FROM responses_new
    WHERE is_correct IS DISTINCT FROM (response_answer = correct_answer);
    IF diff > 0 THEN
        RAISE EXCEPTION '0003: % translated score(s) disagree with the text values', diff;
    END IF;
END;
$$;

-- ---------------------------------------------------------------------------
-- 5. Replace the old tables. Dropping them also drops their 0002 triggers; the
--    protections are recreated below, inside this same transaction.
-- ---------------------------------------------------------------------------
DROP TABLE responses;
DROP TABLE questions;
DROP FUNCTION qa_responses_before_insert();
DROP FUNCTION qa_questions_before_update();
DROP FUNCTION qa_responses_block_mutation();

ALTER TABLE questions_new RENAME TO questions;
ALTER TABLE questions RENAME CONSTRAINT questions_new_pkey TO questions_pkey;
ALTER SEQUENCE questions_new_question_id_seq RENAME TO questions_question_id_seq;

ALTER TABLE responses_new RENAME TO responses;
ALTER TABLE responses RENAME CONSTRAINT responses_new_pkey TO responses_pkey;
ALTER TABLE responses RENAME CONSTRAINT responses_new_question_id_fkey TO responses_question_id_fkey;
ALTER TABLE responses RENAME CONSTRAINT responses_new_question_id_user_id_key TO responses_question_id_user_id_key;
ALTER SEQUENCE responses_new_response_id_seq RENAME TO responses_response_id_seq;

DO $$
BEGIN
    PERFORM setval('questions_question_id_seq', last_value, is_called) FROM qa_0003_seq WHERE t = 'questions';
    PERFORM setval('responses_response_id_seq', last_value, is_called) FROM qa_0003_seq WHERE t = 'responses';
END;
$$;

-- Latest question for a date: WHERE question_date = $1 ORDER BY created_at DESC, question_id DESC LIMIT 1.
CREATE INDEX questions_date_latest_idx ON questions (question_date, created_at DESC, question_id DESC);

COMMENT ON TABLE questions IS 'Quiz questions. Insert-only: several rows may share a date; the newest row for today is served.';
COMMENT ON COLUMN questions.question_date IS 'Business date (Asia/Kolkata). Not unique: a correction is a new row for the same date.';
COMMENT ON COLUMN questions.question IS 'JSONB {"question": text, "options": {"A".."D": text}, "answer": actual correct option text}. Validated by the API; "answer" is never returned to a participant before they respond.';
COMMENT ON TABLE responses IS 'One answer per user per question. Append-only through the application path.';
COMMENT ON COLUMN responses.user_id IS 'Verified corporate email, trimmed and lowercased. Stored data, not authentication.';
COMMENT ON COLUMN responses.user_name IS 'Verified display name at submission time, trimmed.';
COMMENT ON COLUMN responses.question IS 'Question text copied by the trigger from the referenced question payload.';
COMMENT ON COLUMN responses.correct_answer IS 'Actual correct answer text, copied by the trigger from question ->> ''answer'' of the referenced question.';
COMMENT ON COLUMN responses.response_answer IS 'Actual selected answer text (not an option label). Must equal one of the referenced question''s option values.';
COMMENT ON COLUMN responses.is_correct IS 'Computed by trigger; caller values ignored.';
COMMENT ON COLUMN responses.responded_at IS 'Assigned by trigger from clock_timestamp() after the question lock; caller values ignored.';

-- ---------------------------------------------------------------------------
-- 6. Scoring and history rules.
-- ---------------------------------------------------------------------------
-- Locks the exact referenced question FOR SHARE, then reads clock_timestamp() once:
-- that instant is both the eligibility check (is the question dated today in
-- Asia/Kolkata?) and the stored responded_at. The submitted text is matched against
-- the stored option values (trimmed, case-sensitive); the stored option text is saved.
-- The correct answer is read directly from question ->> 'answer' (actual text).
-- SQLSTATEs: QA001 not today, QA002 stored payload unusable, QA003 not an option.
CREATE FUNCTION qa_responses_before_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER  -- FOR SHARE needs UPDATE privilege; the app role should not hold it.
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    q_date   date;
    payload  jsonb;
    opts     jsonb;
    chosen   text;
    correct  text;
    instant  timestamptz;
BEGIN
    NEW.user_id   := lower(regexp_replace(NEW.user_id, '^[[:space:]]+|[[:space:]]+$', '', 'g'));
    NEW.user_name := regexp_replace(NEW.user_name, '^[[:space:]]+|[[:space:]]+$', '', 'g');
    NEW.question       := NULL;
    NEW.correct_answer := NULL;
    NEW.is_correct     := NULL;
    NEW.responded_at   := NULL;

    IF NEW.question_id IS NULL THEN
        RETURN NEW;  -- NOT NULL constraint reports it
    END IF;

    SELECT q.question_date, q.question INTO q_date, payload
    FROM public.questions AS q
    WHERE q.question_id = NEW.question_id
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'question % does not exist', NEW.question_id
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    instant := clock_timestamp();
    IF q_date <> public.qa_business_date(instant) THEN
        RAISE EXCEPTION 'question % is for %, not today (%) in Asia/Kolkata',
                        NEW.question_id, q_date, public.qa_business_date(instant)
            USING ERRCODE = 'QA001';
    END IF;

    -- Fail closed: never score against a payload the API would not have accepted.
    opts := payload -> 'options';
    IF jsonb_typeof(payload) IS DISTINCT FROM 'object'
       OR jsonb_typeof(payload -> 'question') IS DISTINCT FROM 'string'
       OR (payload ->> 'question') !~ '[^[:space:]]'
       OR jsonb_typeof(opts) IS DISTINCT FROM 'object'
       OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(opts) AS k) IS DISTINCT FROM ARRAY['A', 'B', 'C', 'D']
       OR EXISTS (SELECT 1 FROM jsonb_each(opts) AS e
                  WHERE jsonb_typeof(e.value) <> 'string' OR (e.value #>> '{}') !~ '[^[:space:]]')
       OR (SELECT count(DISTINCT btrim(e.value)) FROM jsonb_each_text(opts) AS e) <> 4
       OR jsonb_typeof(payload -> 'answer') IS DISTINCT FROM 'string'
       OR NOT EXISTS (SELECT 1 FROM jsonb_each_text(opts) AS e WHERE e.value = payload ->> 'answer') THEN
        RAISE EXCEPTION 'question % has a malformed payload and cannot be answered', NEW.question_id
            USING ERRCODE = 'QA002';
    END IF;

    IF NEW.response_answer IS NULL THEN
        RETURN NEW;  -- NOT NULL constraint reports it
    END IF;

    correct := payload ->> 'answer';

    SELECT e.value INTO chosen
    FROM jsonb_each_text(opts) AS e
    WHERE btrim(e.value) = btrim(NEW.response_answer);

    IF NOT FOUND THEN
        RAISE EXCEPTION 'response_answer is not one of the options of question %', NEW.question_id
            USING ERRCODE = 'QA003';
    END IF;

    NEW.question        := payload ->> 'question';
    NEW.correct_answer  := correct;
    NEW.response_answer := chosen;
    NEW.is_correct      := (chosen = correct);
    NEW.responded_at    := instant;
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

-- Questions are insert-only, whether or not anyone has answered. A correction is a
-- new row for the same date. Deletion is refused too, preserving history.
CREATE FUNCTION qa_questions_block_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'questions are insert-only: % is not allowed; insert a new row instead', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER questions_block_update_delete
    BEFORE UPDATE OR DELETE ON questions
    FOR EACH ROW EXECUTE FUNCTION qa_questions_block_mutation();

CREATE TRIGGER questions_block_truncate
    BEFORE TRUNCATE ON questions
    FOR EACH STATEMENT EXECUTE FUNCTION qa_questions_block_mutation();

-- ---------------------------------------------------------------------------
-- 7. Final checks on the result.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF (SELECT data_type FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'questions' AND column_name = 'question') <> 'jsonb' THEN
        RAISE EXCEPTION '0003: questions.question is not jsonb';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'questions'::regclass AND contype IN ('u', 'c')) THEN
        RAISE EXCEPTION '0003: questions must have no UNIQUE or CHECK constraints';
    END IF;
    IF (SELECT count(*) FROM pg_trigger WHERE tgrelid IN ('questions'::regclass, 'responses'::regclass) AND NOT tgisinternal) <> 5 THEN
        RAISE EXCEPTION '0003: expected 5 protection triggers';
    END IF;
    IF (SELECT last_value FROM questions_question_id_seq) <> (SELECT last_value FROM qa_0003_seq WHERE t = 'questions')
       OR (SELECT last_value FROM responses_response_id_seq) <> (SELECT last_value FROM qa_0003_seq WHERE t = 'responses') THEN
        RAISE EXCEPTION '0003: identity sequence position changed';
    END IF;
END;
$$;
