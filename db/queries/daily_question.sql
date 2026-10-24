-- Question served for a business date: the newest row for that date, WITHOUT the
-- answer key (what a participant sees). Variable: day (default: today in Asia/Kolkata).
--   scripts/db query daily_question
--   scripts/db query daily_question -v day=2026-10-23
\if :{?day}
\else
\set day ''
\endif
SELECT question_id,
       question_date,
       question ->> 'question' AS question,
       question -> 'options'   AS options,
       created_at
FROM questions
WHERE question_date = COALESCE(NULLIF(:'day', '')::date, qa_business_date(now()))
ORDER BY created_at DESC, question_id DESC
LIMIT 1;
