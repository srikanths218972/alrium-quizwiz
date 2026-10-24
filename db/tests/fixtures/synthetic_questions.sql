-- SYNTHETIC test data for isolated test databases only (backend/tests). Never loaded by the
-- application, setup scripts or migrations.
-- Dates are relative to today in Asia/Kolkata: today-3 .. today+7, with today-2 left
-- empty on purpose to demonstrate the schedule-gap query.
-- Repeatable: a date that already has any question is skipped (questions are insert-only,
-- so nothing is overwritten). Payloads follow the API contract: distinct options, and
-- "answer" is the actual text of the correct option (the letters below only pick it).
-- Responses are not seeded: the database only accepts answers for today's question,
-- so they are created through the API (see docs/POSTMAN-DEMO.md).

INSERT INTO questions (question_date, question)
SELECT qa_business_date(now()) + v.day_offset,
       jsonb_build_object('question', v.question,
                          'options', jsonb_build_object('A', v.a, 'B', v.b, 'C', v.c, 'D', v.d),
                          'answer', CASE v.answer WHEN 'A' THEN v.a WHEN 'B' THEN v.b WHEN 'C' THEN v.c ELSE v.d END)
FROM (VALUES
    (-3, 'Which database does this application use?', 'PostgreSQL', 'MySQL', 'SQLite', 'MongoDB', 'A'),
    (-1, 'What does JSON stand for?', 'Java Source Object Notation', 'JavaScript Object Notation', 'Joined Serial Object Network', 'JavaScript Ordered Nodes', 'B'),
    ( 0, 'What is the capital of India?', 'Mumbai', 'New Delhi', 'Chennai', 'Kolkata', 'B'),
    ( 1, 'Which SQL keyword removes duplicate rows from a result?', 'UNIQUE', 'SINGLE', 'DISTINCT', 'ONLY', 'C'),
    ( 2, 'Which port does PostgreSQL listen on by default?', '3306', '1433', '27017', '5432', 'D'),
    ( 3, 'Which HTTP status code means Not Found?', '200', '301', '404', '500', 'C'),
    ( 4, 'Which planet is known as the Red Planet?', 'Venus', 'Mars', 'Jupiter', 'Mercury', 'B'),
    ( 5, 'How many minutes ahead of UTC is India Standard Time?', '300', '330', '345', '360', 'B'),
    ( 6, 'Which HTML element is used for the largest heading?', '<head>', '<h6>', '<h1>', '<title>', 'C'),
    ( 7, 'Which of these is a version control system?', 'Git', 'Nginx', 'Redis', 'Kafka', 'A')
) AS v (day_offset, question, a, b, c, d, answer)
WHERE NOT EXISTS (SELECT 1 FROM questions AS q WHERE q.question_date = qa_business_date(now()) + v.day_offset);
