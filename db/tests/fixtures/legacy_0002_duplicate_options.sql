-- Adds a 0002 question whose options repeat a value. A letter answer cannot be translated
-- to unambiguous text, so 0003 must refuse it.
INSERT INTO questions (question_date, question, option_a, option_b, option_c, option_d, correct_answer)
VALUES ('2026-10-30', 'Ambiguous', 'Same', 'Same ', 'Other', 'Last', 'A');
