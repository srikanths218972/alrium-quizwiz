
export type Label = 'A' | 'B' | 'C' | 'D';
export const LABELS: Label[] = ['A', 'B', 'C', 'D'];
export type Options = Record<Label, string>;

export interface Me {
  user_id: string;
  user_name: string;
  roles: string[];
  is_admin: boolean;
  auth_mode: 'entra' | 'email' | 'open';
  email_verified: boolean;
  timezone: string;
  business_date: string;
  server_time: string;
}

export interface MyResult {
  response_id: number;
  question_id: number;
  question: string;
  correct_answer: string;
  response_answer: string;
  is_correct: boolean;
  responded_at: string;
}

export interface TodayQuestion {
  question_id: number;
  question_date: string;
  question: { question: string; options: Options };
  created_at: string;
  answered: boolean;
  my_response: MyResult | null;
}

export interface QuestionPayload {
  question: string;
  options: Options;
  answer: string;
}

export interface AdminQuestion {
  question_id: number;
  question_date: string;
  question: QuestionPayload;
  created_at: string;
  response_count: number;
  is_latest_for_date: boolean;
}

export interface AuditItem {
  response_id: number;
  question_id: number;
  question_date: string;
  user_id: string;
  user_name: string;
  question: string;
  correct_answer: string;
  response_answer: string;
  is_correct: boolean;
  responded_at: string;
}

export interface Stats {
  question_id: number;
  correct_answer: string;
  total: number;
  correct: number;
  incorrect: number;
  distribution: Record<Label, number>;
  unmatched: number;
  accuracy: number | null;
}

export interface Page<T> {
  data: T[];
  meta: { page: number; page_size: number; total: number };
}

export interface FieldError {
  field: string;
  message: string;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public fields: FieldError[] = []) {
    super(message);
  }
}

export type HeaderSource = () => Promise<Record<string, string>>;

export function createApi(getHeaders: HeaderSource, base = '') {
  async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
    let res: Response;
    try {
      const identity = await getHeaders();
      res = await fetch(base + path, {
        method,
        headers: { ...identity, ...(body !== undefined ? { 'content-type': 'application/json' } : {}) },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: 'no-store',
      });
    } catch (err) {
      if (err instanceof ApiError) throw err;
      throw new ApiError(0, 'NETWORK_ERROR', 'The server could not be reached. Check your connection.');
    }
    const json = await res.json().catch(() => null);
    if (!res.ok) {
      const e = json?.error;
      throw new ApiError(res.status, e?.code ?? 'HTTP_ERROR', e?.message ?? `Request failed (${res.status}).`, e?.fields ?? []);
    }
    return json as T;
  }

  const q = (params: Record<string, string | number | undefined>) => {
    const entries = Object.entries(params).filter((e): e is [string, string | number] => e[1] !== undefined && e[1] !== '');
    const s = new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString();
    return s ? `?${s}` : '';
  };

  return {
    me: () => request<{ data: Me }>('GET', '/api/v1/me').then((r) => r.data),
    today: () => request<{ data: TodayQuestion }>('GET', '/api/v1/questions/today').then((r) => r.data),
    myResponse: (id: number) => request<{ data: MyResult }>('GET', `/api/v1/questions/${id}/my-response`).then((r) => r.data),
    submit: (id: number, responseAnswer: string) =>
      request<{ data: MyResult }>('POST', `/api/v1/questions/${id}/responses`, { response_answer: responseAnswer }).then((r) => r.data),
    adminQuestions: (p: { from?: string; to?: string; page?: number; page_size?: number }) =>
      request<Page<AdminQuestion>>('GET', `/api/v1/admin/questions${q(p)}`),
    createQuestion: (question_date: string, question: QuestionPayload) =>
      request<{ data: AdminQuestion }>('POST', '/api/v1/admin/questions', { question_date, question }).then((r) => r.data),
    responses: (p: { question_id?: number; page?: number; page_size?: number }) =>
      request<Page<AuditItem>>('GET', `/api/v1/admin/responses${q(p)}`),
    stats: (id: number) => request<{ data: Stats }>('GET', `/api/v1/admin/questions/${id}/stats`).then((r) => r.data),
    gaps: (from: string, to: string) =>
      request<{ data: { missing_dates: string[] } }>('GET', `/api/v1/admin/schedule/gaps${q({ from, to })}`).then((r) => r.data.missing_dates),
  };
}

export type Api = ReturnType<typeof createApi>;
