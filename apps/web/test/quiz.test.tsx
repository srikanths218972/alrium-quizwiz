import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ApiError, type Api, type MyResult, type TodayQuestion } from '../src/api';
import { Quiz } from '../src/components/Quiz';

const TEXT = 'What is the capital of India?';
const QUESTION = {
  question_id: 7,
  question_date: '2026-10-23',
  question: { question: TEXT, options: { A: 'Mumbai', B: 'New Delhi', C: 'Chennai', D: 'Kolkata' } },
  created_at: '2026-10-22T10:00:00+05:30',
};
const CORRECT = 'New Delhi';

interface FakeOptions {
  scheduled?: boolean;
  loseSubmitResponse?: boolean;
  answeredElsewhere?: string;
}

function fakeServer(opts: FakeOptions = {}) {
  let saved: MyResult | null = null;
  const calls: string[] = [];
  const save = (answer: string): MyResult => {
    saved = {
      response_id: 1,
      question_id: QUESTION.question_id,
      question: TEXT,
      correct_answer: CORRECT,
      response_answer: answer,
      is_correct: answer === CORRECT,
      responded_at: '2026-10-23T13:20:01+05:30',
    };
    return saved;
  };
  if (opts.answeredElsewhere) save(opts.answeredElsewhere);

  const api = {
    async today(): Promise<TodayQuestion> {
      calls.push('today');
      if (opts.scheduled === false) throw new ApiError(404, 'QUESTION_NOT_SCHEDULED', 'No question is scheduled for today.');
      const mine = opts.answeredElsewhere && calls.length === 1 ? null : saved;
      return { ...QUESTION, answered: mine !== null, my_response: mine };
    },
    async submit(_id: number, answer: string): Promise<MyResult> {
      calls.push(`submit:${answer}`);
      if (saved) throw new ApiError(409, 'ALREADY_ANSWERED', 'You have already answered this question.');
      const result = save(answer);
      if (opts.loseSubmitResponse) throw new ApiError(0, 'NETWORK_ERROR', 'The server could not be reached.');
      return result;
    },
    async myResponse(): Promise<MyResult> {
      calls.push('my-response');
      if (!saved) throw new ApiError(404, 'RESPONSE_NOT_FOUND', 'No saved response for this question.');
      return saved;
    },
  } as unknown as Api;
  return { api, calls };
}

async function choose(label: string) {
  await userEvent.click(await screen.findByRole('radio', { name: new RegExp(label) }));
}

describe('Quiz', () => {
  it('submits a correct answer, reveals it in green and celebrates', async () => {
    const { api } = fakeServer();
    render(<Quiz api={api} />);
    const submit = await screen.findByRole('button', { name: 'Submit' });
    expect(submit).toBeDisabled();
    expect(screen.queryByText(/Correct answer/)).not.toBeInTheDocument();

    await choose('New Delhi');
    expect(submit).toBeEnabled();
    await userEvent.click(submit);

    expect(await screen.findByText('Correct! Well done.')).toBeInTheDocument();
    const correct = screen.getByTestId('option-B');
    expect(correct).toHaveClass('correct');
    expect(within(correct).getByText(/Correct answer/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Submit' })).not.toBeInTheDocument();
    for (const radio of screen.getAllByRole('radio')) expect(radio).toBeDisabled();
    expect(screen.getByText(/Answered 23 Oct 2026, 1:20/)).toBeInTheDocument();
  });

  it('submits the selected option text, never its label', async () => {
    const server = fakeServer();
    render(<Quiz api={server.api} />);
    await choose('Chennai');
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));
    await screen.findByText(/Not quite/);
    expect(server.calls).toContain('submit:Chennai');
    expect(server.calls.some((c) => /^submit:[ABCD]$/.test(c))).toBe(false);
  });

  it('keeps Submit disabled while the request is pending', async () => {
    let release: (r: MyResult) => void = () => undefined;
    const { api } = fakeServer();
    const slow = { ...api, submit: () => new Promise<MyResult>((r) => { release = r; }) } as Api;
    render(<Quiz api={slow} />);
    await choose('Mumbai');
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));
    expect(screen.getByRole('button', { name: 'Submitting…' })).toBeDisabled();
    release({ response_id: 1, question_id: 7, question: TEXT, correct_answer: CORRECT, response_answer: 'Mumbai', is_correct: false, responded_at: '2026-10-23T13:20:01+05:30' });
    expect(await screen.findByText(/Not quite/)).toBeInTheDocument();
  });

  it('reports an expired session', async () => {
    const { api } = fakeServer();
    let expired = false;
    const failing = { ...api, today: () => Promise.reject(new ApiError(401, 'UNAUTHORIZED', 'expired')) } as Api;
    render(<Quiz api={failing} onExpired={() => { expired = true; }} />);
    expect(await screen.findByText(/session has expired/)).toBeInTheDocument();
    expect(expired).toBe(true);
  });

  it('marks an incorrect choice and gently shows the correct answer', async () => {
    const { api } = fakeServer();
    render(<Quiz api={api} />);
    await choose('Kolkata');
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));

    expect(await screen.findByText(/Not quite this time. The correct answer is New Delhi\./)).toBeInTheDocument();
    expect(screen.getByTestId('option-D')).toHaveClass('wrong');
    expect(within(screen.getByTestId('option-D')).getByText(/Your answer/)).toBeInTheDocument();
    expect(screen.getByTestId('option-B')).toHaveClass('correct');
  });

  it('restores the saved result after a reload instead of allowing another attempt', async () => {
    const server = fakeServer();
    const first = render(<Quiz api={server.api} />);
    await choose('Chennai');
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));
    await screen.findByText(/Not quite/);
    first.unmount();

    render(<Quiz api={server.api} />);
    expect(await screen.findByText(/Not quite/)).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Chennai/ })).toBeChecked();
    expect(screen.queryByRole('button', { name: 'Submit' })).not.toBeInTheDocument();
    expect(server.calls.filter((c) => c.startsWith('submit'))).toHaveLength(1);
  });

  it('recovers from a duplicate submission by showing the existing response', async () => {
    const server = fakeServer({ answeredElsewhere: 'Mumbai' });
    render(<Quiz api={server.api} />);
    await choose('New Delhi');
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));

    expect(await screen.findByText('You had already answered this question.')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /Mumbai/ })).toBeChecked();
    expect(screen.getByTestId('option-A')).toHaveClass('wrong');
    expect(server.calls).toContain('my-response');
  });

  it('recovers from a lost submission response via my-response', async () => {
    const server = fakeServer({ loseSubmitResponse: true });
    render(<Quiz api={server.api} />);
    await choose('New Delhi');
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));

    expect(await screen.findByText('Correct! Well done.')).toBeInTheDocument();
    expect(server.calls).toEqual(['today', 'submit:New Delhi', 'my-response']);
  });

  it('shows a friendly message when no question is scheduled', async () => {
    const { api } = fakeServer({ scheduled: false });
    render(<Quiz api={api} />);
    expect(await screen.findByRole('heading', { name: 'No question today' })).toBeInTheDocument();
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  });

  it('shows an unavailable message and retries when the API is down', async () => {
    let up = false;
    const { api } = fakeServer();
    const flaky = { ...api, today: () => (up ? api.today() : Promise.reject(new ApiError(503, 'SERVICE_UNAVAILABLE', 'down'))) } as Api;
    render(<Quiz api={flaky} />);
    expect(await screen.findByText(/temporarily unavailable/)).toBeInTheDocument();
    up = true;
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByText(TEXT)).toBeInTheDocument());
  });
});
