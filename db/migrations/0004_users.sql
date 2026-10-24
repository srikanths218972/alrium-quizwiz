-- 0004: a users table that outlives questions and responses.
--   * users(user_id, user_name, created_at). user_id is the same identity responses store.
--   * Every response registers its user (ON CONFLICT DO NOTHING), so users appear here
--     even if their answers or questions are later removed.
--   * Users are never deleted: UPDATE, DELETE and TRUNCATE are always refused.
--   * Existing responses are backfilled with each user's most recent user_name.

CREATE TABLE users (
    user_id    text        PRIMARY KEY,
    user_name  text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO users (user_id, user_name, created_at)
SELECT DISTINCT ON (user_id) user_id, user_name, min(responded_at) OVER (PARTITION BY user_id)
FROM responses
ORDER BY user_id, responded_at DESC, response_id DESC;

CREATE FUNCTION qa_responses_register_user()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO users (user_id, user_name) VALUES (NEW.user_id, NEW.user_name)
    ON CONFLICT (user_id) DO NOTHING;
    RETURN NEW;
END;
$$;

CREATE TRIGGER responses_register_user
    AFTER INSERT ON responses
    FOR EACH ROW EXECUTE FUNCTION qa_responses_register_user();

CREATE FUNCTION qa_users_block_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'users are never deleted or changed: % is not allowed', TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$;

CREATE TRIGGER users_block_update_delete
    BEFORE UPDATE OR DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION qa_users_block_mutation();

CREATE TRIGGER users_block_truncate
    BEFORE TRUNCATE ON users
    FOR EACH STATEMENT EXECUTE FUNCTION qa_users_block_mutation();
