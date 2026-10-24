import { useCallback, useEffect, useState } from 'react';
import { ApiError, LABELS, type Api, type MyResult, type TodayQuestion } from '../api';
import { formatBusinessDate, formatDelhiTime } from '../format';

type State =
  | { kind: 'loading' }
  | { kind: 'none' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; question: TodayQuestion; result: MyResult | null; notice?: string };

function describe(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0 || err.status === 503) return 'The quiz service is temporarily unavailable. Please try again in a moment.';
    if (err.status === 401) return 'Your session has expired. Please sign in again.';
    if (err.status === 403) return err.message || 'You do not have access to this quiz.';
    return err.message;
  }
  return 'Something went wrong. Please try again.';
}

export function Quiz({ api, onExpired }: { api: Api; onExpired?: () => void }) {
  const [state, setState] = useState<State>({ kind: 'loading' });
  const [selected, setSelected] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const fail = useCallback((err: unknown) => {
    if (err instanceof ApiError && err.status === 401) onExpired?.();
    return describe(err);
  }, [onExpired]);

  const load = useCallback(async () => {
    setState({ kind: 'loading' });
    try {
      const question = await api.today();
      setState({ kind: 'ready', question, result: question.my_response });
      setSelected(question.my_response?.response_answer ?? null);
    } catch (err) {
      if (err instanceof ApiError && err.code === 'QUESTION_NOT_SCHEDULED') setState({ kind: 'none' });
      else setState({ kind: 'error', message: fail(err) });
    }
  }, [api, fail]);

  useEffect(() => {
    void load();
  }, [load]);

  if (state.kind === 'loading') return <p className="status" role="status">Loading today's question…</p>;
  if (state.kind === 'none') {
    return (
      <section className="card empty" aria-live="polite">
        <h2>No question today</h2>
        <p>There is no question scheduled for today. Please check back tomorrow.</p>
      </section>
    );
  }
  if (state.kind === 'error') {
    return (
      <section className="card" role="alert">
        <p>{state.message}</p>
        <button type="button" onClick={() => void load()}>Try again</button>
      </section>
    );
  }

  const { question, result, notice } = state;
  const locked = result !== null || pending;
  const options = question.question.options;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!selected || locked) return;
    setPending(true);
    setSubmitError(null);
    try {
      const saved = await api.submit(question.question_id, selected);
      setState({ kind: 'ready', question, result: saved });
    } catch (err) {
      const uncertain = !(err instanceof ApiError) || err.status === 0 || err.status >= 500 || err.code === 'ALREADY_ANSWERED';
      if (uncertain) {
        try {
          const saved = await api.myResponse(question.question_id);
          setSelected(saved.response_answer);
          setState({
            kind: 'ready',
            question,
            result: saved,
            notice: err instanceof ApiError && err.code === 'ALREADY_ANSWERED' ? 'You had already answered this question.' : undefined,
          });
          return;
        } catch (lookup) {
          if (!(lookup instanceof ApiError && lookup.code === 'RESPONSE_NOT_FOUND')) {
            setSubmitError('We could not confirm whether your answer was saved. Please reload the page to check.');
            return;
          }
        }
      }
      setSubmitError(fail(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="card quiz">
      <p className="date">{formatBusinessDate(question.question_date)}</p>
      <form onSubmit={onSubmit}>
        <fieldset disabled={locked} aria-describedby={result ? 'feedback' : undefined}>
          <legend className="question">{question.question.question}</legend>
          <div className="options">
            {LABELS.map((label) => {
              const text = options[label];
              const isCorrect = result?.correct_answer === text;
              const isWrongPick = result !== null && result.response_answer === text && !result.is_correct;
              const cls = ['option', selected === text ? 'selected' : '', isCorrect ? 'correct' : '', isWrongPick ? 'wrong' : '']
                .filter(Boolean)
                .join(' ');
              return (
                <label key={label} className={cls} data-testid={`option-${label}`}>
                  <input type="radio" name="answer" value={text} checked={selected === text} onChange={() => setSelected(text)} />
                  <span className="key" aria-hidden="true">{label}</span>
                  <span className="text">{text}</span>
                  {isCorrect && <span className="tag tag-correct"><span aria-hidden="true">✓</span> Correct answer</span>}
                  {isWrongPick && <span className="tag tag-wrong"><span aria-hidden="true">✗</span> Your answer</span>}
                </label>
              );
            })}
          </div>
        </fieldset>
        {!result && (
          <button type="submit" className="primary" disabled={!selected || pending}>
            {pending ? 'Submitting…' : 'Submit'}
          </button>
        )}
      </form>

      {submitError && <p className="error" role="alert">{submitError}</p>}

      {result && (
        <div id="feedback" className={`feedback ${result.is_correct ? 'feedback-correct' : 'feedback-wrong'}`} role="status" aria-live="polite">
          {result.is_correct ? (
            <>
              <div className="celebrate" aria-hidden="true">{Array.from({ length: 12 }, (_, i) => <i key={i} />)}</div>
              <strong>Correct! Well done.</strong>
            </>
          ) : (
            <strong>Not quite this time. The correct answer is {result.correct_answer}.</strong>
          )}
          {notice && <p>{notice}</p>}
          <p className="muted">Answered {formatDelhiTime(result.responded_at)}. Each question can be answered once.</p>
        </div>
      )}
    </section>
  );
}
