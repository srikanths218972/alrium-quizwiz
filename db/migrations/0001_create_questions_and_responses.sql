-- 0001: questions and responses tables, JSON validation, scoring and history rules.
-- Applied by scripts/db migrate inside a single transaction; do not add BEGIN/COMMIT.
-- Portable to PostgreSQL 13+ (gen_random_uuid() is core since 13). Confirm the
-- office server's major version before deployment.

-- ---------------------------------------------------------------------------
-- Question JSON contract
--   {"question": "<nonblank>", "options": {"A": "..","B": "..","C": "..","D": ".."}, "answer": "A".."D"}
-- Exactly these keys, string values, no extras. Always returns true/false, never NULL,
-- so malformed input cannot slip through CHECK's NULL-passes semantics.
-- ---------------------------------------------------------------------------
CREATE FUNCTION qa_is_valid_question(doc jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
PARALLEL SAFE
AS $$
DECLARE
    opt text;
BEGIN
    IF doc IS NULL OR jsonb_typeof(doc) <> 'object' THEN
        RETURN false;
    END IF;

    IF ARRAY(SELECT k FROM jsonb_object_keys(doc) AS k ORDER BY k COLLATE "C")
       IS DISTINCT FROM ARRAY['answer', 'options', 'question'] THEN
        RETURN false;
    END IF;

    IF jsonb_typeof(doc -> 'question') IS DISTINCT FROM 'string'
       OR (doc ->> 'question') ~ '^[[:space:]]*$' THEN
        RETURN false;
    END IF;

    IF jsonb_typeof(doc -> 'options') IS DISTINCT FROM 'object'
       OR ARRAY(SELECT k FROM jsonb_object_keys(doc -> 'options') AS k ORDER BY k COLLATE "C")
          IS DISTINCT FROM ARRAY['A', 'B', 'C', 'D'] THEN
        RETURN false;
    END IF;

    FOREACH opt IN ARRAY ARRAY['A', 'B', 'C', 'D'] LOOP
        IF jsonb_typeof(doc -> 'options' -> opt) IS DISTINCT FROM 'string'
           OR (doc -> 'options' ->> opt) ~ '^[[:space:]]*$' THEN
            RETURN false;
        END IF;
    END LOOP;

    IF jsonb_typeof(doc -> 'answer') IS DISTINCT FROM 'string'
       OR (doc ->> 'answer') NOT IN ('A', 'B', 'C', 'D') THEN
        RETURN false;
    END IF;

    RETURN true;
END;
$$;

COMMENT ON FUNCTION qa_is_valid_question(jsonb) IS
    'Strict validator for question JSON: exact keys question/options/answer, options exactly A-D with nonblank strings, answer in A-D. Never returns NULL.';

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------
CREATE TABLE questions (
    question_id   uuid        NOT NULL DEFAULT gen_random_uuid(),
    question_date date        NOT NULL,
    question      jsonb       NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT questions_pkey PRIMARY KEY (question_id),
    CONSTRAINT questions_question_date_key UNIQUE (question_date),
    CONSTRAINT questions_question_valid CHECK (qa_is_valid_question(question))
);

COMMENT ON TABLE questions IS 'One quiz question per calendar date (business timezone, provisionally Asia/Kolkata).';
COMMENT ON COLUMN questions.question_date IS 'Calendar date the question is scheduled for. UNIQUE: at most one per date; completeness is not enforced.';
COMMENT ON COLUMN questions.question IS 'Question JSON {question, options{A..D}, answer}. Immutable once any response exists.';

CREATE TABLE responses (
    response_id     uuid        NOT NULL DEFAULT gen_random_uuid(),
    question_id     uuid        NOT NULL,
    user_id         text        NOT NULL,
    user_name       text        NOT NULL,
    response_answer text        NOT NULL,
    is_correct      boolean     NOT NULL,
    responded_at    timestamptz NOT NULL,
    CONSTRAINT responses_pkey PRIMARY KEY (response_id),
    CONSTRAINT responses_question_id_fkey FOREIGN KEY (question_id)
        REFERENCES questions (question_id) ON DELETE RESTRICT,
    CONSTRAINT responses_question_id_user_id_key UNIQUE (question_id, user_id),
    -- The trigger normalizes; these checks also reject anything that bypasses it.
    CONSTRAINT responses_user_id_normalized CHECK (
        user_id = lower(user_id)
        AND user_id !~ '[[:space:]]'
        AND user_id ~ '^[^@]+@[^@]+$'
    ),
    CONSTRAINT responses_user_name_nonblank CHECK (
        user_name !~ '^[[:space:]]*$'
        AND user_name !~ '^[[:space:]]|[[:space:]]$'
    ),
    CONSTRAINT responses_response_answer_valid CHECK (response_answer IN ('A', 'B', 'C', 'D'))
);

COMMENT ON TABLE responses IS 'One answer per user per question. Append-only through the application path.';
COMMENT ON COLUMN responses.user_id IS 'Corporate Microsoft email, trimmed and lowercased. Stored data, not authentication.';
COMMENT ON COLUMN responses.user_name IS 'Display-name snapshot at submission time (trimmed).';
COMMENT ON COLUMN responses.is_correct IS 'Computed by trigger from questions.question->>answer; caller values ignored.';
COMMENT ON COLUMN responses.responded_at IS 'Assigned by trigger (statement_timestamp()); caller values ignored.';

-- ---------------------------------------------------------------------------
-- Locking protocol (see db/README.md):
--   * Response insert locks the parent question row FOR SHARE before scoring.
--     Concurrent submissions share the lock; an in-flight question UPDATE/DELETE
--     (which takes a row-exclusive lock) blocks it, and vice versa.
--   * Question UPDATE checks for responses in a BEFORE trigger, which runs after
--     the row lock is acquired, so the check sees any response committed by a
--     transaction it waited on (READ COMMITTED takes a fresh snapshot per statement).
--   * REPEATABLE READ edits are refused: their snapshot predates the wait.
--   The FK check alone takes FOR KEY SHARE, which does not conflict with a
--   non-key UPDATE, so the explicit FOR SHARE is required.
-- ---------------------------------------------------------------------------
CREATE FUNCTION qa_responses_before_insert()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER  -- FOR SHARE needs UPDATE privilege; the app role should not hold it.
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    stored jsonb;
BEGIN
    NEW.user_id   := lower(regexp_replace(NEW.user_id, '^[[:space:]]+|[[:space:]]+$', '', 'g'));
    NEW.user_name := regexp_replace(NEW.user_name, '^[[:space:]]+|[[:space:]]+$', '', 'g');
    NEW.is_correct := NULL;
    NEW.responded_at := statement_timestamp();

    IF NEW.question_id IS NULL THEN
        RETURN NEW;  -- NOT NULL constraint reports it
    END IF;

    SELECT q.question INTO stored
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
        RAISE EXCEPTION 'response_answer must be one of A, B, C, D (got %)', NEW.response_answer
            USING ERRCODE = 'check_violation';
    END IF;

    NEW.is_correct := (NEW.response_answer = stored ->> 'answer');
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

    IF NEW.question IS DISTINCT FROM OLD.question
       OR NEW.question_date IS DISTINCT FROM OLD.question_date THEN
        IF current_setting('transaction_isolation') = 'repeatable read' THEN
            RAISE EXCEPTION 'question edits must run at READ COMMITTED or SERIALIZABLE isolation'
                USING ERRCODE = 'object_not_in_prerequisite_state';
        END IF;

        IF EXISTS (SELECT 1 FROM public.responses AS r WHERE r.question_id = OLD.question_id) THEN
            RAISE EXCEPTION 'question % already has responses; its date and JSON are locked', OLD.question_id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER questions_before_update
    BEFORE UPDATE ON questions
    FOR EACH ROW EXECUTE FUNCTION qa_questions_before_update();
