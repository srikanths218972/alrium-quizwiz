-- History protection: questions are insert-only; responses are append-only.
SELECT qa_test.fresh_question(qa_test.today() + 10, 'Alpha', 'Never answered') AS unanswered_qid \gset
SELECT qa_test.fresh_question(qa_test.today(), 'Alpha', 'Answered') AS answered_qid \gset
INSERT INTO responses (question_id, user_id, user_name, response_answer)
VALUES (:answered_qid, 'hist@alrium.com', 'History User', 'Alpha');

DO $$
DECLARE
    c record;
    q record;
BEGIN
    FOR q IN SELECT * FROM (VALUES ('unanswered', qa_test.today() + 10), ('answered', qa_test.today())) AS t (label, d) LOOP
        FOR c IN SELECT * FROM (VALUES
            ('payload',        $s$question = jsonb_set(question, '{answer}', '"Bravo"')$s$),
            ('question text',  $s$question = jsonb_set(question, '{question}', '"Reworded"')$s$),
            ('question_date',  'question_date = question_date + 30'),
            ('created_at',     $s$created_at = '2000-01-01'$s$),
            ('no-op',          'question = question')
        ) AS t (label, assignment)
        LOOP
            PERFORM qa_test.throws(format('history: %s question %s update refused', q.label, c.label),
                format('UPDATE questions SET %s WHERE question_id = %s', c.assignment, qa_test.latest(q.d)), '23001');
        END LOOP;
        PERFORM qa_test.throws(format('history: %s question delete refused', q.label),
            format('DELETE FROM questions WHERE question_id = %s', qa_test.latest(q.d)), '23001');
    END LOOP;
END;
$$;

SELECT qa_test.throws('history: TRUNCATE questions refused', $$TRUNCATE questions CASCADE$$, '23001');
SELECT qa_test.ok('history: questions unchanged after attempts',
    (SELECT question = qa_test.payload('Alpha', 'Never answered') FROM questions WHERE question_id = :unanswered_qid)
    AND (SELECT question = qa_test.payload('Alpha', 'Answered') FROM questions WHERE question_id = :answered_qid));

SELECT qa_test.throws('history: response answer update blocked',
    $$UPDATE responses SET response_answer = 'Bravo' WHERE user_id = 'hist@alrium.com'$$, '23001');
SELECT qa_test.throws('history: response text update blocked',
    $$UPDATE responses SET correct_answer = 'Bravo' WHERE user_id = 'hist@alrium.com'$$, '23001');
SELECT qa_test.throws('history: response is_correct update blocked',
    $$UPDATE responses SET is_correct = NOT is_correct WHERE user_id = 'hist@alrium.com'$$, '23001');
SELECT qa_test.throws('history: response timestamp update blocked',
    $$UPDATE responses SET responded_at = now() WHERE user_id = 'hist@alrium.com'$$, '23001');
SELECT qa_test.throws('history: response delete blocked',
    $$DELETE FROM responses WHERE user_id = 'hist@alrium.com'$$, '23001');
SELECT qa_test.throws('history: responses TRUNCATE blocked',
    $$TRUNCATE responses$$, '23001');
SELECT qa_test.throws('history: upsert cannot overwrite a response',
    format($$INSERT INTO responses (question_id, user_id, user_name, response_answer)
             VALUES (%s, 'hist@alrium.com', 'History User', 'Bravo')
             ON CONFLICT (question_id, user_id) DO UPDATE SET response_answer = EXCLUDED.response_answer$$, :answered_qid), '23001');
SELECT qa_test.ok('history: response unchanged after attempts',
    (SELECT response_answer = 'Alpha' AND correct_answer = 'Alpha' AND is_correct FROM responses WHERE user_id = 'hist@alrium.com'));

-- A newer version for the same date leaves existing responses untouched.
INSERT INTO questions (question_date, question) VALUES (qa_test.today(), qa_test.payload('Charlie', 'Answered v2'));
SELECT qa_test.ok('history: newer version does not rewrite older responses',
    (SELECT question_id = :answered_qid AND question = 'Answered' AND correct_answer = 'Alpha' AND is_correct
     FROM responses WHERE user_id = 'hist@alrium.com'));
