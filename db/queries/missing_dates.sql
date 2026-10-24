-- Dates in [from, to] with no question at all. Several questions may share a date;
-- this query checks completeness of the schedule.
--   scripts/db query missing_dates -v from=2026-10-19 -v to=2026-10-31
SELECT d::date AS missing_date
FROM generate_series(:'from'::date, :'to'::date, interval '1 day') AS d
WHERE NOT EXISTS (SELECT 1 FROM questions AS q WHERE q.question_date = d::date)
ORDER BY 1;
