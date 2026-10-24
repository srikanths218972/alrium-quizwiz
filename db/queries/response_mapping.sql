-- Read-only: who answered which question, with what, and the score. Run with `make db-shell`.
-- user_id is the normalized email from the validated session. In email mode it is self-declared
-- (not verified) and user_name equals user_id; in Microsoft mode user_name is the Entra display name.
SELECT r.response_id,
       r.user_id,
       r.user_name,
       r.question_id,
       q.question_date,
       q.created_at        AS question_created_at,
       r.question          AS question_text_at_submission,
       r.response_answer   AS selected_answer,
       r.correct_answer,
       r.is_correct,
       r.responded_at
FROM responses r
JOIN questions q USING (question_id)
ORDER BY q.question_date DESC, r.question_id, r.responded_at, r.response_id;
