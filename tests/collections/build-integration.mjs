import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';


const here = dirname(fileURLToPath(import.meta.url));
const lines = (s) => s.trim().split('\n');

const TS = `const isDelhiTs = (s) => {
  if (typeof s !== 'string' || !/^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\+05:30$/.test(s)) return false;
  const t = Date.parse(s);
  return !Number.isNaN(t) && new Date(t + 330 * 60000).toISOString().slice(0, 19) === s.slice(0, 19);
};`;

function request(name, method, path, { token, body, tests = '', pre = '', description, headers = [], auth } = {}) {
  return { name, method, path, token, body, tests, pre, description, headers, auth };
}
const folder = (name, description, items) => ({ name, description, items });

const PERSONAS = {
  'employee-correct': 'first.last@example.test',
  'employee-wrong': 'second.person@example.test',
  'employee-unanswered': 'third.person+quiz@example.test',
};
const tokenVar = (p) => `token_${p.replace(/-/g, '_')}`;
const expectedEmail = (p) => `(pm.environment.get('expected_email_${p.replace(/-/g, '_')}') || '${PERSONAS[p]}').toLowerCase()`;

const CAPITAL = { A: 'Mumbai', B: 'New Delhi', C: 'Chennai', D: 'Kolkata' };
const V1 = { question: 'What is the capital of India?', options: CAPITAL, answer: 'New Delhi' };
const V2 = { question: 'Which city was formerly called Madras?', options: CAPITAL, answer: 'Chennai' };
const create = (dateVar, p) => ({ question_date: `{{${dateVar}}}`, question: p });
const answerBody = (value) => `{\n  "response_answer": "${value}"\n}`;

const expectStatus = (code, errorCode) => `
pm.test('HTTP ${code}${errorCode ? ` ${errorCode}` : ''}', () => {
  pm.expect(pm.response.code).to.eql(${code});
  ${errorCode ? `pm.expect(pm.response.json().error.code).to.eql('${errorCode}');` : ''}
});`;

const saveQuestion = (prefix) => `
const q = pm.response.json().data;
pm.test('created as a new integer ID with the exact payload', () => {
  pm.expect(Number.isInteger(q.question_id)).to.eql(true);
  pm.expect(q.question_date).to.eql(pm.environment.get('current_date'));
  pm.expect(Object.values(q.question.options)).to.include(q.question.answer);
  pm.expect(q.response_count).to.eql(0);
  pm.expect(q.is_latest_for_date).to.eql(true);
  pm.expect(isDelhiTs(q.created_at)).to.eql(true);
});
pm.environment.set('${prefix}_id', q.question_id);
pm.environment.set('${prefix}_text', q.question.question);
pm.environment.set('${prefix}_answer', q.question.answer);
pm.environment.set('${prefix}_wrong', Object.values(q.question.options).find((v) => v !== q.question.answer));`;

const collectionPre = `
// Fresh Delhi business dates before every request (India has no daylight saving time: UTC+05:30).
const delhi = (offsetDays) => new Date(Date.now() + 330 * 60000 + offsetDays * 86400000).toISOString().slice(0, 10);
pm.environment.set('current_date', delhi(0));
pm.environment.set('past_date', delhi(-1));
pm.environment.set('future_date', delhi(1));`;

const collectionTests = `
// Shared checks for every response.
const apiPath = pm.request.url.getPath();
if (pm.response.code >= 400 && apiPath.startsWith('/api/')) {
  pm.test('error envelope has code, message and request_id', () => {
    const e = pm.response.json().error;
    pm.expect(e.code).to.be.a('string');
    pm.expect(e.message).to.be.a('string');
    pm.expect(e.request_id).to.be.a('string');
  });
  pm.test('no SQL or stack details in errors', () => {
    pm.expect(pm.response.text()).to.not.match(/Traceback|SELECT |constraint|psycopg|QA00/i);
  });
}
if (apiPath.startsWith('/api/')) {
  pm.test('Cache-Control: no-store', () => pm.expect(pm.response.headers.get('Cache-Control')).to.eql('no-store'));
}`;

const noLeak = `
pm.test('no answer before answering', () => {
  const d = pm.response.json().data;
  pm.expect(Object.keys(d.question).sort()).to.eql(['options', 'question']);
  pm.expect(Object.keys(d.question.options).sort()).to.eql(['A', 'B', 'C', 'D']);
  pm.expect(d.answered).to.eql(false);
  pm.expect(d.my_response).to.eql(null);
  pm.expect(pm.response.text()).to.not.match(/"answer"|correct_answer|is_correct/);
});`;

const savedResult = (prefix, selected, correct) => `
const d = pm.response.json().data;
pm.test('saved result: exact texts, IDs, correctness and Delhi timestamp', () => {
  pm.expect(Object.keys(d).sort()).to.eql(['correct_answer', 'is_correct', 'question', 'question_id', 'responded_at', 'response_answer', 'response_id']);
  pm.expect(Number.isInteger(d.response_id)).to.eql(true);
  pm.expect(d.question_id).to.eql(Number(pm.environment.get('${prefix}_id')));
  pm.expect(d.question).to.eql(pm.environment.get('${prefix}_text'));
  pm.expect(d.correct_answer).to.eql(pm.environment.get('${prefix}_answer'));
  pm.expect(d.response_answer).to.eql(pm.environment.get('${selected}'));
  pm.expect(d.is_correct).to.eql(${correct});
  pm.expect(isDelhiTs(d.responded_at)).to.eql(true);
});`;


const health = folder('Health', 'Liveness has no database dependency; readiness checks the database and the required migration (0004).', [
  request('Liveness', 'GET', '/health/live', { tests: `pm.test('200 ok', () => pm.expect(pm.response.json()).to.eql({ status: 'ok' }));` }),
  request('Readiness (database + migration 0004)', 'GET', '/health/ready', {
    tests: `pm.test('200 ready at 0004', () => { pm.expect(pm.response.code).to.eql(200); pm.expect(pm.response.json()).to.eql({ status: 'ready', migration: '0004' }); });`,
  }),
]);

const entraAuth = folder(
  'Authentication (Microsoft Entra, manual)',
  'Real sign-in through the Postman OAuth 2.0 helper: Authorization Code with PKCE, public client, no client secret. Open this request, ' +
    'Authorization tab, "Get New Access Token", sign in as the intended user, then copy the ACCESS token (not the ID token) into the ' +
    'matching token_* variable: three different non-admin accounts into the employee variables. Set expected_email_* to those accounts. ' +
    'Admin requests in this suite use X-Admin-Key (email mode); in Microsoft mode check admin access with the client collection and admin_token. ' +
    'See docs/POSTMAN.md.',
  [
    request('Entra: who am I (uses OAuth helper token)', 'GET', '/api/v1/me', {
      auth: {
        type: 'oauth2',
        oauth2: [
          { key: 'grant_type', value: 'authorization_code_with_pkce', type: 'string' },
          { key: 'authUrl', value: 'https://login.microsoftonline.com/{{tenant_id}}/oauth2/v2.0/authorize', type: 'string' },
          { key: 'accessTokenUrl', value: 'https://login.microsoftonline.com/{{tenant_id}}/oauth2/v2.0/token', type: 'string' },
          { key: 'clientId', value: '{{postman_client_id}}', type: 'string' },
          { key: 'scope', value: '{{api_scope}} openid profile offline_access', type: 'string' },
          { key: 'redirect_uri', value: '{{callback_url}}', type: 'string' },
          { key: 'useBrowser', value: false, type: 'boolean' },
          { key: 'challengeAlgorithm', value: 'S256', type: 'string' },
          { key: 'client_authentication', value: 'body', type: 'string' },
          { key: 'addTokenTo', value: 'header', type: 'string' },
          { key: 'tokenName', value: 'ålrium - QuizWiz API', type: 'string' },
        ],
      },
      tests: `pm.test('signed in with Entra', () => { pm.expect(pm.response.code).to.eql(200); pm.expect(pm.response.json().data.auth_mode).to.eql('entra'); });`,
    }),
  ],
);

const identities = folder(
  'Identity check (read only)',
  'Each token variable must resolve to its expected identity and role. Guards against pasting a token into the wrong variable.',
  Object.keys(PERSONAS).map((persona) =>
    request(`Who am I: ${persona}`, 'GET', '/api/v1/me', {
      token: tokenVar(persona),
      tests: `${TS}
pm.test('identity matches ${tokenVar(persona)}', () => {
  pm.expect(pm.response.code).to.eql(200);
  const me = pm.response.json().data;
  pm.expect(me.user_id).to.eql(${expectedEmail(persona)});
  pm.expect(me.is_admin).to.eql(false);
  pm.expect(me.user_name.toLowerCase()).to.not.include('demo');
  pm.expect(me.timezone).to.eql('Asia/Kolkata');
  pm.expect(me.business_date).to.eql(pm.environment.get('current_date'));
  pm.expect(isDelhiTs(me.server_time)).to.eql(true);
});`,
    }),
  ),
);


const readOnlyAny = folder(
  'Read-only (works on an empty database)',
  'GET requests only; safe against any database, including the working one. Passes whether or not questions exist.',
  [
    request('All questions, unfiltered, page 1 (admin)', 'GET', '/api/v1/admin/questions?page=1&page_size=20', {
      token: 'token_admin',
      tests: `${TS}
pm.test('200 paginated, no date filter required', () => {
  pm.expect(pm.response.code).to.eql(200);
  const b = pm.response.json();
  pm.expect(b.meta.page).to.eql(1);
  pm.expect(b.meta.page_size).to.eql(20);
  pm.expect(b.meta.total).to.be.at.least(b.data.length);
  pm.expect(b.data.length).to.be.at.most(20);
});
pm.test('full payload with an actual-text answer', () => {
  pm.response.json().data.forEach((q) => {
    pm.expect(Object.keys(q.question).sort()).to.eql(['answer', 'options', 'question']);
    pm.expect(Object.values(q.question.options)).to.include(q.question.answer);
    pm.expect(typeof q.is_latest_for_date).to.eql('boolean');
    pm.expect(isDelhiTs(q.created_at)).to.eql(true);
  });
});
pm.test('ordered by date, created_at, question_id', () => {
  const key = (q) => [q.question_date, q.created_at, String(q.question_id).padStart(10, '0')].join('|');
  const rows = pm.response.json().data;
  for (let i = 1; i < rows.length; i++) pm.expect(key(rows[i - 1]) <= key(rows[i])).to.eql(true);
});`,
    }),
    request('All questions as an employee is 403', 'GET', '/api/v1/admin/questions', {
      token: 'token_employee_unanswered',
      tests: `${expectStatus(403, 'FORBIDDEN')}
pm.test('no question content leaked', () => pm.expect(pm.response.text()).to.not.match(/options|"answer"/));`,
    }),
    request("Today's question: 200, or 404 when none is scheduled", 'GET', '/api/v1/questions/today', {
      token: 'token_employee_unanswered',
      tests: `
pm.test('200 or 404 QUESTION_NOT_SCHEDULED', () => pm.expect([200, 404]).to.include(pm.response.code));
if (pm.response.code === 404) pm.test('404 code', () => pm.expect(pm.response.json().error.code).to.eql('QUESTION_NOT_SCHEDULED'));
else pm.test('never includes the answer in the question', () => pm.expect(pm.response.json().data.question).to.not.have.property('answer'));`,
    }),
    request('My saved history (employee-correct)', 'GET', '/api/v1/me/responses?page_size=5', {
      token: 'token_employee_correct',
      tests: `${TS}
pm.test('200 with text values and Delhi timestamps', () => {
  pm.expect(pm.response.code).to.eql(200);
  pm.response.json().data.forEach((r) => {
    ['question', 'correct_answer', 'response_answer'].forEach((k) => pm.expect(r[k]).to.be.a('string'));
    pm.expect(isDelhiTs(r.responded_at)).to.eql(true);
  });
});`,
    }),
  ],
);

const readOnlyPopulated = folder(
  'Read-only (requires a question for today)',
  "GET requests only. Fails with a clear message when nothing is scheduled today; run the WRITES folders first on a test database, " +
    'or insert a question deliberately (POST /api/v1/admin/questions).',
  [
    request("Today's question exists and hides the answer", 'GET', '/api/v1/questions/today', {
      token: 'token_employee_unanswered',
      tests: `${TS}
pm.test('a question is scheduled for today (otherwise run the WRITES folders on a test database)', () => pm.expect(pm.response.code).to.eql(200));
const d = pm.response.json().data;
pm.test('nested question with four options for today', () => {
  pm.expect(d.question_date).to.eql(pm.environment.get('current_date'));
  pm.expect(d.question.question).to.be.a('string').and.not.empty;
  ['A', 'B', 'C', 'D'].forEach((k) => pm.expect(d.question.options[k]).to.be.a('string').and.not.empty);
  pm.expect(d.question).to.not.have.property('answer');
  pm.expect(isDelhiTs(d.created_at)).to.eql(true);
});
if (!d.answered) pm.test('no correct answer before answering', () => pm.expect(pm.response.text()).to.not.match(/correct_answer|is_correct/));`,
    }),
  ],
);


const WRITE_NOTE =
  'WRITES. Run only against a disposable test database (make test-postman does this) or when you deliberately selected an environment ' +
  "where these rows should exist: questions are insert-only, so every row created here stays, and inserting for today CHANGES TODAY'S " +
  'QUESTION for everyone. ';

const findOnPage = (idVar, position) => [
  request(`Count today's questions (for ${idVar})`, 'GET', '/api/v1/admin/questions?from={{current_date}}&to={{current_date}}&page_size=1', {
    token: 'token_admin',
    tests: `
const total = pm.response.json().meta.total;
pm.test('both versions counted', () => pm.expect(total).to.be.at.least(2));
pm.environment.set('find_page', String(Math.floor((total - ${position}) / 20) + 1));`,
  }),
  request(`Find ${idVar} on its page`, 'GET', '/api/v1/admin/questions?from={{current_date}}&to={{current_date}}&page={{find_page}}&page_size=20', {
    token: 'token_admin',
    tests: `
const prefix = '${idVar}'.replace('_id', '');
const q = pm.response.json().data.find((r) => r.question_id === Number(pm.environment.get('${idVar}')));
pm.test('${idVar} is still stored, unchanged', () => {
  pm.expect(q, 'question on page ' + pm.environment.get('find_page')).to.be.an('object');
  pm.expect(q.question.question).to.eql(pm.environment.get(prefix + '_text'));
  pm.expect(q.question.answer).to.eql(pm.environment.get(prefix + '_answer'));
});
pm.test('only the newest version is latest', () => pm.expect(q.is_latest_for_date).to.eql(prefix === 'v2'));`,
  }),
];

const history = folder(
  'WRITES 1: Same-date versions and history',
  WRITE_NOTE + 'Version 1 is inserted and answered; version 2 (different question and answer) is inserted for the same date. ' +
    "Today's endpoint must serve version 2 and version 1's saved response must stay exactly as it was.",
  [
    request('Insert version 1 for today', 'POST', '/api/v1/admin/questions', {
      token: 'token_admin', body: create('current_date', V1), tests: `${TS}${expectStatus(201)}${saveQuestion('v1')}
pm.test('answer is the actual text, not a label', () => pm.expect(q.question.answer).to.eql('New Delhi'));`,
    }),
    request("Today's question is version 1", 'GET', '/api/v1/questions/today', {
      token: 'token_employee_correct',
      tests: `${expectStatus(200)}
pm.test('version 1 served with its exact text', () => {
  const d = pm.response.json().data;
  pm.expect(d.question_id).to.eql(Number(pm.environment.get('v1_id')));
  pm.expect(d.question.question).to.eql(pm.environment.get('v1_text'));
});
${noLeak}`,
    }),
    request('Answer version 1 correctly (employee-correct)', 'POST', '/api/v1/questions/{{v1_id}}/responses', {
      token: 'token_employee_correct', body: answerBody('{{v1_answer}}'),
      tests: `${TS}${expectStatus(201)}${savedResult('v1', 'v1_answer', true)}
pm.environment.set('v1_saved', JSON.stringify(d));`,
    }),
    request('Insert version 2 for the same date (different question and answer)', 'POST', '/api/v1/admin/questions', {
      token: 'token_admin', body: create('current_date', V2), tests: `${TS}${expectStatus(201)}${saveQuestion('v2')}
pm.test('distinct, higher ID than version 1', () => pm.expect(q.question_id).to.be.above(Number(pm.environment.get('v1_id'))));`,
    }),
    request("Today's question is now version 2", 'GET', '/api/v1/questions/today', {
      token: 'token_employee_unanswered',
      tests: `${expectStatus(200)}
pm.test('newest row selected (created_at DESC, question_id DESC)', () => {
  const d = pm.response.json().data;
  pm.expect(d.question_id).to.eql(Number(pm.environment.get('v2_id')));
  pm.expect(d.question.question).to.eql(pm.environment.get('v2_text'));
});
${noLeak}`,
    }),
    request("Version 1's saved response is unchanged", 'GET', '/api/v1/questions/{{v1_id}}/my-response', {
      token: 'token_employee_correct',
      tests: `${expectStatus(200)}
pm.test('identical to the response saved before version 2 existed', () => pm.expect(pm.response.json().data).to.eql(JSON.parse(pm.environment.get('v1_saved'))));`,
    }),
    ...findOnPage('v1_id', 2),
    ...findOnPage('v2_id', 1),
  ],
);

const submissions = folder(
  'WRITES 2: Correct and incorrect submissions (version 2)',
  WRITE_NOTE + 'Answers are sent as option TEXT by different users. Each run answers a fresh version 2, so the folder is repeatable.',
  [
    request('Submit the correct answer text (employee-correct)', 'POST', '/api/v1/questions/{{v2_id}}/responses', {
      token: 'token_employee_correct', body: answerBody('{{v2_answer}}'),
      tests: `${TS}${expectStatus(201)}${savedResult('v2', 'v2_answer', true)}
pm.environment.set('v2_saved', JSON.stringify(d));`,
    }),
    request('Submit an incorrect answer text (employee-wrong)', 'POST', '/api/v1/questions/{{v2_id}}/responses', {
      token: 'token_employee_wrong', body: answerBody('{{v2_wrong}}'),
      tests: `${TS}${expectStatus(201)}${savedResult('v2', 'v2_wrong', false)}`,
    }),
    request('Repeat submission is 409 (employee-correct)', 'POST', '/api/v1/questions/{{v2_id}}/responses', {
      token: 'token_employee_correct', body: answerBody('{{v2_wrong}}'),
      tests: `${expectStatus(409, 'ALREADY_ANSWERED')}
pm.test('message', () => pm.expect(pm.response.json().error.message).to.eql('You have already answered this question.'));`,
    }),
    request('Saved result unchanged after the repeat', 'GET', '/api/v1/questions/{{v2_id}}/my-response', {
      token: 'token_employee_correct',
      tests: `${expectStatus(200)}
pm.test('identical to the first saved response', () => pm.expect(pm.response.json().data).to.eql(JSON.parse(pm.environment.get('v2_saved'))));`,
    }),
    request("Today's question after answering (employee-wrong)", 'GET', '/api/v1/questions/today', {
      token: 'token_employee_wrong',
      tests: `${expectStatus(200)}
pm.test('answered state restored with saved texts', () => {
  const d = pm.response.json().data;
  pm.expect(d.question_id).to.eql(Number(pm.environment.get('v2_id')));
  pm.expect(d.answered).to.eql(true);
  pm.expect(d.my_response.response_answer).to.eql(pm.environment.get('v2_wrong'));
  pm.expect(d.my_response.correct_answer).to.eql(pm.environment.get('v2_answer'));
  pm.expect(d.my_response.is_correct).to.eql(false);
  pm.expect(d.question).to.not.have.property('answer');
});`,
    }),
    request('History includes version 2 (employee-correct)', 'GET', '/api/v1/me/responses?from={{current_date}}&to={{current_date}}&page_size=100', {
      token: 'token_employee_correct',
      tests: `${expectStatus(200)}
pm.test('both answered versions listed with their own texts', () => {
  const rows = pm.response.json().data;
  const by = Object.fromEntries(rows.map((r) => [r.question_id, r]));
  pm.expect(by[pm.environment.get('v2_id')].correct_answer).to.eql(pm.environment.get('v2_answer'));
  pm.expect(by[pm.environment.get('v1_id')].correct_answer).to.eql(pm.environment.get('v1_answer'));
});`,
    }),
    request('Audit list for version 2 (admin)', 'GET', '/api/v1/admin/responses?question_id={{v2_id}}', {
      token: 'token_admin',
      tests: `${expectStatus(200)}
pm.test('both users stored with names and actual texts', () => {
  const rows = pm.response.json().data;
  pm.expect(rows).to.have.lengthOf(2);
  const by = Object.fromEntries(rows.map((r) => [r.user_id, r]));
  const right = by[${expectedEmail('employee-correct')}];
  const wrong = by[${expectedEmail('employee-wrong')}];
  pm.expect(right.response_answer).to.eql(pm.environment.get('v2_answer'));
  pm.expect(right.is_correct).to.eql(true);
  pm.expect(wrong.response_answer).to.eql(pm.environment.get('v2_wrong'));
  pm.expect(wrong.correct_answer).to.eql(pm.environment.get('v2_answer'));
  pm.expect(wrong.is_correct).to.eql(false);
  rows.forEach((r) => pm.expect(r.question).to.eql(pm.environment.get('v2_text')));
  if (!pm.environment.get('expected_email_employee_correct')) {
    // Email mode: the display name is the email itself.
    pm.expect(right.user_name).to.eql(right.user_id);
    pm.expect(wrong.user_name).to.eql(wrong.user_id);
  }
});`,
    }),
    request('Statistics for version 2 (admin)', 'GET', '/api/v1/admin/questions/{{v2_id}}/stats', {
      token: 'token_admin',
      tests: `${expectStatus(200)}
pm.test('two responses, one correct, distribution mapped from text to labels', () => {
  const s = pm.response.json().data;
  pm.expect(s.correct_answer).to.eql(pm.environment.get('v2_answer'));
  pm.expect([s.total, s.correct, s.incorrect, s.unmatched, s.accuracy]).to.eql([2, 1, 1, 0, 0.5]);
  pm.expect(s.distribution.A + s.distribution.B + s.distribution.C + s.distribution.D).to.eql(2);
});
pm.environment.set('v2_total', pm.response.json().data.total);`,
    }),
  ],
);

const replacement = folder(
  'WRITES 3: Replacement policy (answering an older version)',
  WRITE_NOTE +
    'Proposed policy for review (the meeting did not settle it): a question shown earlier today can still be answered by its exact ID ' +
    'after a newer version exists, and is scored against that ID only. One response per question, not per day.',
  [
    request('Older version 1 still answerable by its ID (employee-unanswered)', 'POST', '/api/v1/questions/{{v1_id}}/responses', {
      token: 'token_employee_unanswered', body: answerBody('{{v1_wrong}}'),
      tests: `${TS}${expectStatus(201)}${savedResult('v1', 'v1_wrong', false)}
pm.test('scored against version 1, never switched to version 2', () => pm.expect(d.correct_answer).to.not.eql(pm.environment.get('v2_answer')));`,
    }),
  ],
);

const malformed = [
  ['missing option D', { question: 'q', options: { A: 'a', B: 'b', C: 'c' }, answer: 'a' }, 'question.options.D'],
  ['extra option E', { question: 'q', options: { A: 'a', B: 'b', C: 'c', D: 'd', E: 'e' }, answer: 'a' }, 'question.options.E'],
  ['answer given as label B', V1 && { ...V1, answer: 'B' }, 'question.answer'],
  ['answer not an option', { ...V1, answer: 'Bangalore' }, 'question.answer'],
  ['duplicate option values', { question: 'q', options: { A: 'Same', B: ' Same ', C: 'c', D: 'd' }, answer: 'c' }, 'question.options'],
  ['blank question text', { question: '   ', options: { A: 'a', B: 'b', C: 'c', D: 'd' }, answer: 'a' }, 'question.question'],
  ['options as an array', { question: 'q', options: ['a', 'b', 'c', 'd'], answer: 'a' }, 'question.options'],
  ['unknown payload field', { ...V1, hint: 'x' }, 'question.hint'],
];

const forged = [
  ['question text', { response_answer: 'Mumbai', question: 'Forged question' }, 'question'],
  ['correct_answer', { response_answer: 'Mumbai', correct_answer: 'Mumbai' }, 'correct_answer'],
  ['identity', { response_answer: 'Mumbai', user_id: 'someone.else@alrium.com', user_name: 'Someone Else' }, 'user_id'],
  ['is_correct', { response_answer: 'Mumbai', is_correct: true }, 'is_correct'],
  ['responded_at', { response_answer: 'Mumbai', responded_at: '2026-01-01T00:00:00+05:30' }, 'responded_at'],
  ['response_id and question_id', { response_answer: 'Mumbai', response_id: 1, question_id: 1 }, 'response_id'],
];

const negative = folder(
  'WRITES 4: Negative tests',
  WRITE_NOTE +
    "Every request must be rejected with the documented status. Inserts one future-dated and one past-dated fixture (which do not change " +
    "today's question) to check the today-only rule, and one future question whose options are literally A-D. Ends by checking nothing else was written.",
  [
    request('Count stored questions before', 'GET', '/api/v1/admin/questions?page_size=1', {
      token: 'token_admin', tests: `pm.environment.set('questions_before', pm.response.json().meta.total);`,
    }),
    request('Missing auth is 401', 'GET', '/api/v1/questions/today', { tests: expectStatus(401, 'UNAUTHORIZED') }),
    request('Garbage token is 401', 'GET', '/api/v1/me', {
      headers: [{ key: 'Authorization', value: 'Bearer aaa.bbb.ccc' }], tests: expectStatus(401, 'UNAUTHORIZED'),
    }),
    request('Missing auth on submit is 401', 'POST', '/api/v1/questions/{{v2_id}}/responses', { body: answerBody('Mumbai'), tests: expectStatus(401) }),
    request('Employee cannot insert questions (403)', 'POST', '/api/v1/admin/questions', {
      token: 'token_employee_wrong', body: create('future_date', V1), tests: expectStatus(403, 'FORBIDDEN'),
    }),
    request('Employee cannot read stats (403)', 'GET', '/api/v1/admin/questions/{{v2_id}}/stats', {
      token: 'token_employee_wrong', tests: expectStatus(403, 'FORBIDDEN'),
    }),
    request('Unknown question ID is 404', 'POST', '/api/v1/questions/2147480000/responses', {
      token: 'token_employee_unanswered', body: answerBody('Mumbai'), tests: expectStatus(404, 'QUESTION_NOT_FOUND'),
    }),
    request('Option label "B" instead of its text is 400', 'POST', '/api/v1/questions/{{v2_id}}/responses', {
      token: 'token_employee_unanswered', body: answerBody('B'),
      tests: `${expectStatus(400, 'VALIDATION_ERROR')}
pm.test('names response_answer and leaks no answer', () => {
  pm.expect(pm.response.json().error.fields.map((f) => f.field)).to.include('response_answer');
  pm.expect(pm.response.text()).to.not.include(pm.environment.get('v2_answer'));
});`,
    }),
    request('Value that is not an option is 400', 'POST', '/api/v1/questions/{{v2_id}}/responses', {
      token: 'token_employee_unanswered', body: answerBody('Bangalore'), tests: expectStatus(400, 'VALIDATION_ERROR'),
    }),
    ...forged.map(([label, body, field]) =>
      request(`Forged ${label} in the submission is 400`, 'POST', '/api/v1/questions/{{v2_id}}/responses', {
        token: 'token_employee_unanswered', body,
        tests: `${expectStatus(400, 'VALIDATION_ERROR')}
pm.test('unknown field ${field}', () => pm.expect(pm.response.json().error.fields).to.deep.include({ field: '${field}', message: 'unknown field' }));`,
      }),
    ),
    request('Malformed JSON body is 400', 'POST', '/api/v1/questions/{{v2_id}}/responses', {
      token: 'token_employee_unanswered', body: '{"response_answer":', tests: expectStatus(400, 'VALIDATION_ERROR'),
    }),
    request('Employee-unanswered has not answered version 2', 'GET', '/api/v1/questions/{{v2_id}}/my-response', {
      token: 'token_employee_unanswered',
      tests: `${expectStatus(404, 'RESPONSE_NOT_FOUND')}
pm.test('no answer leaked', () => pm.expect(pm.response.text()).to.not.include(pm.environment.get('v2_answer')));`,
    }),
    ...['PATCH', 'PUT', 'DELETE'].map((method) =>
      request(`${method} a question is 405 (insert-only)`, method, '/api/v1/admin/questions/{{v2_id}}', {
        token: 'token_admin', ...(method === 'DELETE' ? {} : { body: { question: V1 } }), tests: expectStatus(405, 'QUESTIONS_INSERT_ONLY'),
      }),
    ),
    ...malformed.map(([label, p, field]) =>
      request(`Malformed question: ${label} is 400`, 'POST', '/api/v1/admin/questions', {
        token: 'token_admin', body: create('future_date', p),
        tests: `${expectStatus(400, 'VALIDATION_ERROR')}
pm.test('names ${field}', () => pm.expect(pm.response.json().error.fields.map((f) => f.field)).to.include('${field}'));`,
      }),
    ),
    request('Old flat question format is 400', 'POST', '/api/v1/admin/questions', {
      token: 'token_admin',
      body: { question_date: '{{future_date}}', question: 'q', option_a: 'a', option_b: 'b', option_c: 'c', option_d: 'd', correct_answer: 'A' },
      tests: expectStatus(400, 'VALIDATION_ERROR'),
    }),
    request('Impossible date is 400', 'POST', '/api/v1/admin/questions', {
      token: 'token_admin', body: { question_date: '2026-02-30', question: V1 }, tests: expectStatus(400, 'VALIDATION_ERROR'),
    }),
    request('No question rows written by rejected requests', 'GET', '/api/v1/admin/questions?page_size=1', {
      token: 'token_admin',
      tests: `pm.test('question count unchanged', () => pm.expect(pm.response.json().meta.total).to.eql(Number(pm.environment.get('questions_before'))));`,
    }),
    request('Single-letter answer accepted when it IS an option value (future fixture)', 'POST', '/api/v1/admin/questions', {
      token: 'token_admin',
      body: create('future_date', { question: 'Which letter comes second?', options: { A: 'A', B: 'B', C: 'C', D: 'D' }, answer: 'B' }),
      tests: `${expectStatus(201)}
pm.test('stored answer is the text "B" of option B', () => pm.expect(pm.response.json().data.question.answer).to.eql('B'));
pm.environment.set('future_id', pm.response.json().data.question_id);`,
    }),
    request('Future question cannot be answered (403)', 'POST', '/api/v1/questions/{{future_id}}/responses', {
      token: 'token_employee_unanswered', body: answerBody('B'), tests: expectStatus(403, 'QUESTION_NOT_OPEN'),
    }),
    request('Insert a past-dated question (fixture)', 'POST', '/api/v1/admin/questions', {
      token: 'token_admin', body: create('past_date', { ...V1, question: 'Past question (closed)' }),
      tests: `${expectStatus(201)}
pm.environment.set('past_id', pm.response.json().data.question_id);`,
    }),
    request('Past question cannot be answered (403)', 'POST', '/api/v1/questions/{{past_id}}/responses', {
      token: 'token_employee_unanswered', body: answerBody('New Delhi'), tests: expectStatus(403, 'QUESTION_NOT_OPEN'),
    }),
    request('Version 2 responses unchanged by the negative tests', 'GET', '/api/v1/admin/questions/{{v2_id}}/stats', {
      token: 'token_admin',
      tests: `pm.test('still exactly the responses from WRITES 2', () => pm.expect(pm.response.json().data.total).to.eql(Number(pm.environment.get('v2_total'))));`,
    }),
  ],
);

const replay = folder('Replay (read only)', 'Non-destructive replay of saved results after a write run (needs v1_id/v2_id). Safe to repeat.', [
  ...['employee-correct', 'employee-wrong'].map((p) =>
    request(`Saved result on version 2: ${p}`, 'GET', '/api/v1/questions/{{v2_id}}/my-response', {
      token: tokenVar(p),
      tests: `${TS}
pm.test('200 with a persisted text result', () => {
  pm.expect(pm.response.code).to.eql(200);
  const d = pm.response.json().data;
  pm.expect(d.correct_answer).to.eql(pm.environment.get('v2_answer'));
  pm.expect(isDelhiTs(d.responded_at)).to.eql(true);
});`,
    }),
  ),
]);

const FOLDERS = [health, entraAuth, identities, readOnlyAny, readOnlyPopulated, history, submissions, replacement, negative, replay];
const DESCRIPTION =
  'ålrium - QuizWiz API (FastAPI). Questions are JSONB {"question","options":{"A".."D"},"answer"} where answer is the actual option text; ' +
  'several versions may share a date and the newest is served; questions are insert-only. Core operations: GET /api/v1/questions/today, ' +
  'GET /api/v1/admin/questions (all questions, admin), POST /api/v1/questions/{id}/responses with {"response_answer":"<option text>"}. ' +
  "Folders named WRITES insert rows and change today's question: run them only against a disposable test database (make test-postman) " +
  'unless you deliberately selected that environment. Read-only folders are safe on the working database. See docs/POSTMAN.md. ' +
  'Contains no tokens or keys.';


function postmanItem(r) {
  const header = [...r.headers];
  if (r.token === 'token_admin') header.push({ key: 'X-Admin-Key', value: '{{admin_key}}' });
  else if (r.token) header.push({ key: 'Authorization', value: `Bearer {{${r.token}}}` });
  if (r.body !== undefined) header.push({ key: 'Content-Type', value: 'application/json' });
  const [p, query] = r.path.split('?');
  const item = {
    name: r.name,
    request: {
      method: r.method,
      header,
      url: {
        raw: `{{base_url}}${r.path}`,
        host: ['{{base_url}}'],
        path: p.split('/').filter(Boolean),
        ...(query ? { query: query.split('&').map((kv) => ({ key: kv.split('=')[0], value: kv.split('=').slice(1).join('=') })) } : {}),
      },
      ...(r.description ? { description: r.description } : {}),
      ...(r.body !== undefined ? { body: { mode: 'raw', raw: typeof r.body === 'string' ? r.body : JSON.stringify(r.body, null, 2) } } : {}),
      ...(r.auth ? { auth: r.auth } : r.token ? {} : { auth: { type: 'noauth' } }),
    },
    event: [],
  };
  if (r.pre) item.event.push({ listen: 'prerequest', script: { type: 'text/javascript', exec: lines(r.pre) } });
  if (r.tests) item.event.push({ listen: 'test', script: { type: 'text/javascript', exec: lines(r.tests) } });
  return item;
}

const collection = {
  info: { name: 'ålrium - QuizWiz API', description: DESCRIPTION, schema: 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json' },
  event: [
    { listen: 'prerequest', script: { type: 'text/javascript', exec: lines(collectionPre) } },
    { listen: 'test', script: { type: 'text/javascript', exec: lines(collectionTests) } },
  ],
  item: FOLDERS.map((f) => ({ name: f.name, description: f.description, item: f.items.map(postmanItem) })),
};

const VARIABLES = [
  ['base_url', 'http://localhost:8000', false],
  ['admin_key', '', true],
  ['tenant_id', '', false],
  ['postman_client_id', '', false],
  ['api_scope', 'api://<api-client-id>/Quiz.Participate', false],
  ['callback_url', 'https://oauth.pstmn.io/v1/callback', false],
  ['token_employee_correct', '', true],
  ['token_employee_wrong', '', true],
  ['token_employee_unanswered', '', true],
  ['expected_email_employee_correct', '', false],
  ['expected_email_employee_wrong', '', false],
  ['expected_email_employee_unanswered', '', false],
];
const environment = {
  name: 'ålrium - QuizWiz local',
  values: VARIABLES.map(([key, value, secret]) => ({ key, value, type: secret ? 'secret' : 'default', enabled: true })),
  _postman_variable_scope: 'environment',
};

mkdirSync(join(here, 'integration'), { recursive: true });
writeFileSync(join(here, 'integration', 'question_answer.postman_collection.json'), JSON.stringify(collection, null, 2) + '\n');
writeFileSync(join(here, 'integration', 'question_answer.postman_environment.json'), JSON.stringify(environment, null, 2) + '\n');

console.log(`Generated integration collection (${collection.item.reduce((n, f) => n + f.item.length, 0)} requests).`);
