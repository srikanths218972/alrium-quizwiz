import { useCallback, useEffect, useState } from 'react';
import { ApiError, LABELS, type AdminQuestion, type Api, type AuditItem, type Label, type Options, type Stats } from '../api';
import { LIMITS } from '../config';
import { addDays, formatDelhiTime } from '../format';

interface Draft {
  question_date: string;
  question: string;
  options: Options;
  correct: Label | null;
}

const emptyDraft = (date: string): Draft => ({ question_date: date, question: '', options: { A: '', B: '', C: '', D: '' }, correct: null });
const PAGE_SIZE = 20;

function problems(d: Draft): string[] {
  const out: string[] = [];
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d.question_date)) out.push('Choose a date.');
  if (!d.question.trim()) out.push('Enter the question.');
  const values = LABELS.map((l) => d.options[l].trim());
  if (values.some((v) => !v)) out.push('Fill in all four options.');
  else if (new Set(values).size !== 4) out.push('Options must all be different.');
  if (!d.correct) out.push('Select the correct option.');
  return out;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const fields = err.fields.map((f) => `${f.field}: ${f.message}`).join('; ');
    return fields ? `${err.message} (${fields})` : err.message;
  }
  return 'Something went wrong.';
}

export function Admin({ api, today }: { api: Api; today: string }) {
  const [range, setRange] = useState({ from: addDays(today, -7), to: addDays(today, 30) });
  const [page, setPage] = useState(1);
  const [list, setList] = useState<{ rows: AdminQuestion[]; total: number } | null>(null);
  const [listError, setListError] = useState('');
  const [draft, setDraft] = useState<Draft>(emptyDraft(today));
  const [replacing, setReplacing] = useState<AdminQuestion | null>(null);
  const [saving, setSaving] = useState(false);
  const [formMessage, setFormMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [selected, setSelected] = useState<AdminQuestion | null>(null);
  const [detail, setDetail] = useState<{ stats: Stats; responses: AuditItem[] } | null>(null);
  const [gaps, setGaps] = useState<string[] | null>(null);

  const loadList = useCallback(async () => {
    setListError('');
    try {
      const res = await api.adminQuestions({ from: range.from || undefined, to: range.to || undefined, page, page_size: PAGE_SIZE });
      setList({ rows: res.data, total: res.meta.total });
    } catch (err) {
      setListError(errorText(err));
    }
  }, [api, range, page]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  useEffect(() => {
    api.gaps(today, addDays(today, 30)).then(setGaps, () => setGaps(null));
  }, [api, today]);

  async function select(q: AdminQuestion) {
    setSelected(q);
    setDetail(null);
    try {
      const [stats, responses] = await Promise.all([api.stats(q.question_id), api.responses({ question_id: q.question_id, page_size: 100 })]);
      setDetail({ stats, responses: responses.data });
    } catch (err) {
      setListError(errorText(err));
    }
  }

  function startReplacement(q: AdminQuestion) {
    const correct = LABELS.find((l) => q.question.options[l] === q.question.answer) ?? null;
    setReplacing(q);
    setDraft({ question_date: q.question_date, question: q.question.question, options: { ...q.question.options }, correct });
    setFormMessage(null);
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    const issues = problems(draft);
    if (issues.length) {
      setFormMessage({ ok: false, text: issues.join(' ') });
      return;
    }
    setSaving(true);
    setFormMessage(null);
    try {
      const options = Object.fromEntries(LABELS.map((l) => [l, draft.options[l].trim()])) as Options;
      const created = await api.createQuestion(draft.question_date, { question: draft.question.trim(), options, answer: options[draft.correct!] });
      setFormMessage({ ok: true, text: `Saved as question ${created.question_id} for ${created.question_date}.` });
      setDraft(emptyDraft(draft.question_date));
      setReplacing(null);
      await loadList();
    } catch (err) {
      setFormMessage({ ok: false, text: errorText(err) });
    } finally {
      setSaving(false);
    }
  }

  const pages = list ? Math.max(1, Math.ceil(list.total / PAGE_SIZE)) : 1;

  return (
    <div className="admin">
      <section className="card">
        <h2>Questions</h2>
        <p className="muted">Questions cannot be edited. A correction is a new version for the same date; the newest version is served.</p>
        <div className="range">
          <label>From <input type="date" value={range.from} onChange={(e) => { setPage(1); setRange({ ...range, from: e.target.value }); }} /></label>
          <label>To <input type="date" value={range.to} onChange={(e) => { setPage(1); setRange({ ...range, to: e.target.value }); }} /></label>
          <button type="button" onClick={() => { setPage(1); setRange({ from: '', to: '' }); }}>All dates</button>
        </div>
        {listError && <p className="error" role="alert">{listError}</p>}
        {!list ? (
          <p role="status">Loading…</p>
        ) : list.rows.length === 0 ? (
          <p>No questions in this range.</p>
        ) : (
          <table>
            <thead>
              <tr><th>Date</th><th>ID</th><th>Question</th><th>Answer</th><th>Responses</th><th><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {list.rows.map((q) => (
                <tr key={q.question_id} className={q.question_date === today && q.is_latest_for_date ? 'current' : ''}>
                  <td>{q.question_date}</td>
                  <td>{q.question_id}{q.is_latest_for_date ? <span className="badge">Latest</span> : <span className="badge muted-badge">Older</span>}</td>
                  <td>
                    <button type="button" className="link" onClick={() => void select(q)}>{q.question.question}</button>
                    <div className="muted small">Created {formatDelhiTime(q.created_at)}</div>
                  </td>
                  <td>{q.question.answer}</td>
                  <td>{q.response_count}</td>
                  <td><button type="button" onClick={() => startReplacement(q)}>Create replacement</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {list && pages > 1 && (
          <div className="actions">
            <button type="button" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
            <span>Page {page} of {pages}</span>
            <button type="button" disabled={page >= pages} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        )}
      </section>

      <section className="card editor">
        <h2>{replacing ? `Replacement for question ${replacing.question_id}` : 'Add a question'}</h2>
        {replacing && <p className="notice">This creates a new question. Question {replacing.question_id} and its responses stay unchanged.</p>}
        <form onSubmit={save} noValidate>
          <fieldset disabled={saving}>
            <label>Date <input type="date" required value={draft.question_date} onChange={(e) => setDraft({ ...draft, question_date: e.target.value })} /></label>
            <label>Question
              <textarea required maxLength={LIMITS.question} rows={3} value={draft.question} onChange={(e) => setDraft({ ...draft, question: e.target.value })} />
            </label>
            {LABELS.map((l) => (
              <label key={l}>Option {l}
                <input required maxLength={LIMITS.option} value={draft.options[l]}
                  onChange={(e) => setDraft({ ...draft, options: { ...draft.options, [l]: e.target.value } })} />
              </label>
            ))}
            <div className="correct-picker" role="radiogroup" aria-label="Correct option">
              <span>Correct option:</span>
              {LABELS.map((l) => (
                <label key={l}>
                  <input type="radio" name="correct" checked={draft.correct === l} onChange={() => setDraft({ ...draft, correct: l })} /> {l}
                </label>
              ))}
            </div>
            <div className="actions">
              <button type="submit" className="primary">{saving ? 'Saving…' : 'Save as new question'}</button>
              {replacing && <button type="button" onClick={() => { setReplacing(null); setDraft(emptyDraft(today)); }}>Cancel</button>}
            </div>
          </fieldset>
        </form>
        {formMessage && <p className={formMessage.ok ? 'ok' : 'error'} role={formMessage.ok ? 'status' : 'alert'}>{formMessage.text}</p>}
      </section>

      {selected && (
        <section className="card stats" aria-live="polite">
          <h2>Question {selected.question_id}: responses</h2>
          {!detail ? (
            <p role="status">Loading…</p>
          ) : (
            <>
              <p>{detail.stats.total} responses, {detail.stats.correct} correct
                {detail.stats.accuracy !== null && ` (${Math.round(detail.stats.accuracy * 100)}%)`}. Correct answer: {detail.stats.correct_answer}.</p>
              <ul>
                {LABELS.map((l) => (
                  <li key={l}>
                    <span>{l}: {selected.question.options[l]}</span>
                    <span className="bar" style={{ width: `${detail.stats.total ? (detail.stats.distribution[l] / detail.stats.total) * 100 : 0}%` }} />
                    <span>{detail.stats.distribution[l]}</span>
                  </li>
                ))}
              </ul>
              {detail.responses.length > 0 && (
                <table>
                  <thead><tr><th>Name</th><th>Email</th><th>Answer</th><th>Result</th><th>Answered</th></tr></thead>
                  <tbody>
                    {detail.responses.map((r) => (
                      <tr key={r.response_id}>
                        <td>{r.user_name}</td><td>{r.user_id}</td><td>{r.response_answer}</td>
                        <td>{r.is_correct ? 'Correct' : 'Incorrect'}</td><td>{formatDelhiTime(r.responded_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </section>
      )}

      <section className="card">
        <h2>Dates without a question (next 30 days)</h2>
        {gaps === null ? <p className="muted">Unavailable.</p> : gaps.length === 0 ? <p>Every date has a question.</p> : (
          <ul className="gaps">{gaps.map((d) => <li key={d}>{d}</li>)}</ul>
        )}
      </section>
    </div>
  );
}
