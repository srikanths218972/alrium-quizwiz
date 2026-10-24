-- Response history, newest first, timestamps shown in Asia/Kolkata. Answers are stored text.
-- Optional filters: user (email, normalized here), from, to (dates).
--   scripts/db query response_history
--   scripts/db query response_history -v user=someone@example.com -v from=2026-10-01
\if :{?user}
\else
\set user ''
\endif
\if :{?from}
\else
\set from ''
\endif
\if :{?to}
\else
\set to ''
\endif
SELECT r.response_id,
       to_char(r.responded_at AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD"T"HH24:MI:SS"+05:30"') AS responded_at,
       q.question_date,
       r.user_id,
       r.user_name,
       r.question,
       r.correct_answer,
       r.response_answer,
       r.is_correct
FROM responses AS r
JOIN questions AS q USING (question_id)
WHERE (NULLIF(:'user', '') IS NULL OR r.user_id = lower(btrim(:'user')))
  AND (NULLIF(:'from', '') IS NULL OR q.question_date >= NULLIF(:'from', '')::date)
  AND (NULLIF(:'to', '')   IS NULL OR q.question_date <= NULLIF(:'to', '')::date)
ORDER BY r.responded_at DESC, r.response_id DESC;
