-- Per-question totals and option distribution (read only). Responses store option
-- text, so each one is mapped back to its key through the question's own options.
--   scripts/db query question_stats
SELECT q.question_id,
       q.question_date,
       count(r.response_id)                                                    AS total,
       count(*) FILTER (WHERE r.is_correct)                                    AS correct,
       count(*) FILTER (WHERE NOT r.is_correct)                                AS incorrect,
       count(*) FILTER (WHERE r.response_answer = q.question -> 'options' ->> 'A') AS a,
       count(*) FILTER (WHERE r.response_answer = q.question -> 'options' ->> 'B') AS b,
       count(*) FILTER (WHERE r.response_answer = q.question -> 'options' ->> 'C') AS c,
       count(*) FILTER (WHERE r.response_answer = q.question -> 'options' ->> 'D') AS d
FROM questions AS q
LEFT JOIN responses AS r USING (question_id)
GROUP BY q.question_id, q.question_date
ORDER BY q.question_date DESC, q.created_at DESC, q.question_id DESC;
