import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { AdminQuestion, Api, QuestionPayload } from '../src/api';
import { Admin } from '../src/components/Admin';

const options = { A: 'Mumbai', B: 'New Delhi', C: 'Chennai', D: 'Kolkata' };
const row = (id: number, latest: boolean, text: string, answer: string): AdminQuestion => ({
  question_id: id, question_date: '2026-10-23', question: { question: text, options, answer },
  created_at: '2026-10-23T09:00:00+05:30', response_count: id === 1 ? 2 : 0, is_latest_for_date: latest,
});

function fakeApi() {
  const created: { date: string; question: QuestionPayload }[] = [];
  const rows = [row(1, false, 'Old version', 'New Delhi'), row(2, true, 'Corrected version', 'Chennai')];
  const api = {
    adminQuestions: async () => ({ data: rows, meta: { page: 1, page_size: 20, total: rows.length } }),
    createQuestion: async (date: string, question: QuestionPayload) => {
      created.push({ date, question });
      return { ...row(3, true, question.question, question.answer), question_date: date };
    },
    gaps: async () => ['2026-10-25'],
    stats: async () => ({ question_id: 1, correct_answer: 'New Delhi', total: 2, correct: 1, incorrect: 1, distribution: { A: 1, B: 1, C: 0, D: 0 }, unmatched: 0, accuracy: 0.5 }),
    responses: async () => ({
      data: [{ response_id: 9, question_id: 1, question_date: '2026-10-23', user_id: 'rahul.sharma@alrium.com', user_name: 'Rahul Sharma',
        question: 'Old version', correct_answer: 'New Delhi', response_answer: 'Mumbai', is_correct: false, responded_at: '2026-10-23T10:00:00+05:30' }],
      meta: { page: 1, page_size: 100, total: 1 },
    }),
  } as unknown as Api;
  return { api, created };
}

describe('Admin', () => {
  it('lists versions and marks only the latest', async () => {
    render(<Admin api={fakeApi().api} today="2026-10-23" />);
    const table = await screen.findByRole('table');
    const rows = within(table).getAllByRole('row').slice(1);
    expect(within(rows[0]!).getByText('Older')).toBeInTheDocument();
    expect(within(rows[1]!).getByText('Latest')).toBeInTheDocument();
    expect(await screen.findByText('2026-10-25')).toBeInTheDocument();
  });

  it('converts the chosen correct option to its actual text before inserting', async () => {
    const { api, created } = fakeApi();
    render(<Admin api={api} today="2026-10-23" />);
    await userEvent.type(await screen.findByLabelText('Question'), 'Which planet is red?');
    for (const [l, v] of [['A', 'Venus'], ['B', 'Mars'], ['C', 'Jupiter'], ['D', 'Mercury']]) {
      await userEvent.type(screen.getByLabelText(`Option ${l}`), v!);
    }
    await userEvent.click(within(screen.getByRole('radiogroup', { name: 'Correct option' })).getByLabelText('B'));
    await userEvent.click(screen.getByRole('button', { name: 'Save as new question' }));
    expect(await screen.findByText(/Saved as question 3/)).toBeInTheDocument();
    expect(created[0]!.question).toEqual({ question: 'Which planet is red?', options: { A: 'Venus', B: 'Mars', C: 'Jupiter', D: 'Mercury' }, answer: 'Mars' });
  });

  it('refuses duplicate options before calling the API', async () => {
    const { api, created } = fakeApi();
    render(<Admin api={api} today="2026-10-23" />);
    await userEvent.type(await screen.findByLabelText('Question'), 'Q?');
    for (const l of ['A', 'B', 'C', 'D']) await userEvent.type(screen.getByLabelText(`Option ${l}`), l === 'D' ? 'same' : l === 'C' ? 'same' : l);
    await userEvent.click(within(screen.getByRole('radiogroup', { name: 'Correct option' })).getByLabelText('A'));
    await userEvent.click(screen.getByRole('button', { name: 'Save as new question' }));
    expect(await screen.findByText(/Options must all be different/)).toBeInTheDocument();
    expect(created).toHaveLength(0);
  });

  it('"Create replacement" prefills a new insert and never edits the original', async () => {
    const { api, created } = fakeApi();
    render(<Admin api={api} today="2026-10-23" />);
    const buttons = await screen.findAllByRole('button', { name: 'Create replacement' });
    await userEvent.click(buttons[1]!);
    expect(screen.getByText(/Question 2 and its responses stay unchanged/)).toBeInTheDocument();
    expect(screen.getByLabelText('Question')).toHaveValue('Corrected version');
    expect(within(screen.getByRole('radiogroup', { name: 'Correct option' })).getByLabelText('C')).toBeChecked();
    await userEvent.clear(screen.getByLabelText('Question'));
    await userEvent.type(screen.getByLabelText('Question'), 'Third version');
    await userEvent.click(screen.getByRole('button', { name: 'Save as new question' }));
    expect(await screen.findByText(/Saved as question 3/)).toBeInTheDocument();
    expect(created[0]).toEqual({ date: '2026-10-23', question: { question: 'Third version', options, answer: 'Chennai' } });
  });

  it('shows responses and stats for a question in Delhi time', async () => {
    render(<Admin api={fakeApi().api} today="2026-10-23" />);
    await userEvent.click(await screen.findByRole('button', { name: 'Old version' }));
    expect(await screen.findByText(/2 responses, 1 correct \(50%\)/)).toBeInTheDocument();
    expect(screen.getByText('Rahul Sharma')).toBeInTheDocument();
    expect(screen.getByText(/23 Oct 2026, 10:00 am IST/)).toBeInTheDocument();
  });
});
