import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const NAME = 'ålrium - QuizWiz';


const BASE = `const stop = (msg) => { pm.execution.skipRequest(); throw new Error(msg); };
const need = (key, label) => { const v = String(pm.variables.get(key) ?? '').trim(); if (!v) stop('Set ' + key + ' in the selected environment: ' + label); return v; };
const body = (() => { try { return JSON.parse(pm.request.body?.raw || '{}'); } catch (e) { stop('The Body tab is not valid JSON'); } })();
const text = (v) => (typeof v === 'string' ? v.trim() : '');`;

const SIGN_IN = `${BASE}
need('email', 'the email to sign up or sign in with');`;

const ADMIN = `${BASE}
need('admin_key', 'ADMIN_API_KEY from the server .env');`;

const PARTICIPANT = `${BASE}
need('token', 'send Sign up or Sign in first');`;

const CREATE = `${ADMIN}
if (!/^\\d{4}-\\d{2}-\\d{2}$/.test(text(body.question_date))) stop('Fill "question_date" in the Body tab as YYYY-MM-DD');
const q = body.question || {};
const opts = q.options || {};
if (!text(q.question)) stop('Fill "question" in the Body tab');
if (!['A', 'B', 'C', 'D'].every((k) => text(opts[k]))) stop('Fill all four options A to D in the Body tab');
if (!text(q.answer)) stop('Fill "answer" in the Body tab with the exact text of the correct option');
if (!Object.values(opts).map(text).includes(text(q.answer))) stop('"answer" must equal the text of one option');`;

const NEED_ID = `const questionId = need('question_id', 'send Get question of the day first');
if (!/^[1-9][0-9]{0,9}$/.test(questionId)) stop('question_id must be a positive integer');`;

const RESPOND = `${PARTICIPANT}
${NEED_ID}
if (!text(body.response_answer)) stop('Fill "response_answer" in the Body tab with the exact text of the chosen option');`;

const NEED_DATE = `${ADMIN}
if (!/^\\d{4}-\\d{2}-\\d{2}$/.test(need('date', 'the question date to report, YYYY-MM-DD'))) stop('date must be YYYY-MM-DD');`;

const SAVE_TOKEN = `if (pm.response.code === 200 || pm.response.code === 201) {
  const token = pm.response.json()?.data?.access_token;
  if (token) pm.environment.set('token', token);
}`;

const SAVE_ID = `if (pm.response.code === 200 || pm.response.code === 201) {
  const id = pm.response.json()?.data?.question_id;
  if (Number.isInteger(id) && id > 0) pm.environment.set('question_id', String(id));
}`;

const EMAIL_BODY = { email: '{{email}}' };
const CREATE_BODY = { question_date: '', question: { question: '', options: { A: '', B: '', C: '', D: '' }, answer: '' } };
const RESPONSE_BODY = { response_answer: '' };


const admin = { auth: 'admin', pre: ADMIN };
const participant = { auth: 'bearer', pre: PARTICIPANT };
const FOLDERS = [
  ['Sign-in', [
    ['Auth mode', 'GET', '/api/v1/auth/mode'],
    ['Sign up', 'POST', '/api/v1/auth/email-signup', { body: EMAIL_BODY, pre: SIGN_IN, after: SAVE_TOKEN }],
    ['Sign in', 'POST', '/api/v1/auth/email-login', { body: EMAIL_BODY, pre: SIGN_IN, after: SAVE_TOKEN }],
  ]],
  ['Admin', [
    ['List all users', 'GET', '/api/v1/admin/users?page={{page}}&page_size={{page_size}}', admin],
    ['All responses', 'GET', '/api/v1/admin/responses?page={{page}}&page_size={{page_size}}', admin],
    ['Responses to question', 'GET', '/api/v1/admin/responses?question_id={{question_id}}&page={{page}}&page_size={{page_size}}',
      { auth: 'admin', pre: `${ADMIN}\n${NEED_ID}` }],
    ['Answers by date', 'GET', '/api/v1/admin/answers/by-date?date={{date}}', { auth: 'admin', pre: NEED_DATE }],
    ['Get all questions', 'GET', '/api/v1/admin/questions?page={{page}}&page_size={{page_size}}', admin],
    ['Create question', 'POST', '/api/v1/admin/questions', { auth: 'admin', body: CREATE_BODY, pre: CREATE, after: SAVE_ID }],
  ]],
  ['Participant', [
    ['Get question of the day', 'GET', '/api/v1/questions/today', { ...participant, after: SAVE_ID }],
    ['Post response', 'POST', '/api/v1/questions/{{question_id}}/responses', { auth: 'bearer', body: RESPONSE_BODY, pre: RESPOND }],
  ]],
];

const VARIABLES = [
  ['base_url', 'http://localhost:8000', false],
  ['email', '', false],
  ['token', '', true],
  ['admin_key', '', true],
  ['question_id', '', false],
  ['date', '', false],
  ['page', '1', false],
  ['page_size', '20', false],
];


const script = (listen, code) => ({ listen, script: { type: 'text/javascript', exec: code.split('\n') } });
function postmanItem([name, method, path, { auth, body, pre, after } = {}]) {
  const request = { method, header: [], auth: { type: 'noauth' }, url: `{{base_url}}${path}` };
  if (auth === 'bearer') request.auth = { type: 'bearer', bearer: [{ key: 'token', value: '{{token}}', type: 'string' }] };
  if (auth === 'admin') request.header.push({ key: 'X-Admin-Key', value: '{{admin_key}}' });
  if (body) {
    request.header.push({ key: 'Content-Type', value: 'application/json' });
    request.body = { mode: 'raw', raw: JSON.stringify(body, null, 2), options: { raw: { language: 'json' } } };
  }
  return { name, request, event: [...(pre ? [script('prerequest', pre)] : []), ...(after ? [script('test', after)] : [])] };
}
const collection = {
  info: { name: NAME, schema: 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json' },
  item: FOLDERS.map(([name, items]) => ({ name, item: items.map(postmanItem) })),
};
const environment = {
  name: `${NAME} local`,
  values: VARIABLES.map(([key, value, secret]) => ({ key, value, type: secret ? 'secret' : 'default', enabled: true })),
  _postman_variable_scope: 'environment',
};
const json = (file, data) => writeFileSync(join(here, file), JSON.stringify(data, null, 2) + '\n');
json('quizwiz.postman_collection.json', collection);
json('quizwiz.postman_environment.json', environment);

const count = FOLDERS.reduce((n, [, items]) => n + items.length, 0);
console.log(`Generated ${count} requests in ${FOLDERS.length} folders.`);
