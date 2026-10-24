import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { createEmailAuth, type AuthClient } from '../src/auth';
import type { RuntimeConfig } from '../src/config';

const base: RuntimeConfig = { appName: 'ålrium - QuizWiz', authMode: 'email', tenantId: '', clientId: '', apiScope: '', redirectUri: '/' };

function fakeEntra(fail = false): AuthClient {
  let signedIn = false;
  return {
    mode: 'entra',
    init: async () => signedIn,
    login: async () => {
      if (fail) throw new Error('Microsoft sign-in failed.');
      return (signedIn = true);
    },
    logout: async () => void (signedIn = false),
    headers: async () => ({ authorization: 'Bearer header.payload.signature' }),
  };
}

type Route = { status: number; body: unknown };
function mockFetch(routes: Record<string, Route | ((init?: RequestInit) => Route)>) {
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    const r = routes[url];
    const res = typeof r === 'function' ? r(init) : r ?? { status: 404, body: { error: { code: 'NOT_FOUND', message: 'x' } } };
    return new Response(JSON.stringify(res.body), { status: res.status, headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

const me = (isAdmin: boolean, mode = 'email') => ({
  status: 200,
  body: { data: { user_id: 'first.last@example.test', user_name: 'first.last@example.test', roles: ['employee'], is_admin: isAdmin, auth_mode: mode, email_verified: mode === 'entra', timezone: 'Asia/Kolkata', business_date: '2026-10-23' } },
});
const none = { status: 404, body: { error: { code: 'QUESTION_NOT_SCHEDULED', message: 'none' } } };
const login = { status: 200, body: { data: { access_token: 'a.b.c', token_type: 'Bearer', expires_in: 3600, user_id: 'first.last@example.test' } } };

describe('App shell', () => {
  it('email mode shows only an email field, the notice and Sign in; no password, no Microsoft button', async () => {
    mockFetch({});
    render(<App config={base} />);
    expect(screen.getByRole('heading', { name: 'ålrium - QuizWiz' })).toBeInTheDocument();
    expect(await screen.findByText('Sign in with the email you signed up with. Email ownership is not verified.')).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toHaveAttribute('type', 'email');
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByText('Sign in with Microsoft')).not.toBeInTheDocument();
  });

  it('email sign-in sends only the typed email, then uses the returned token', async () => {
    const fetch = mockFetch({ '/api/v1/auth/email-login': login, '/api/v1/me': me(false), '/api/v1/questions/today': none });
    render(<App config={base} />);
    await userEvent.type(await screen.findByLabelText('Email'), '  First.Last@Example.test ');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('first.last@example.test')).toBeInTheDocument();
    const [, init] = fetch.mock.calls.find(([u]) => u === '/api/v1/auth/email-login')!;
    expect(JSON.parse(String(init!.body))).toEqual({ email: 'First.Last@Example.test' });
    const [, meInit] = fetch.mock.calls.find(([u]) => u === '/api/v1/me')!;
    expect((meInit!.headers as Record<string, string>).authorization).toBe('Bearer a.b.c');
    expect(screen.queryByRole('button', { name: 'Admin' })).not.toBeInTheDocument();
  });

  it('rejects blank email without calling the API, and shows server validation errors', async () => {
    const fetch = mockFetch({ '/api/v1/auth/email-login': { status: 400, body: { error: { code: 'VALIDATION_ERROR', message: 'Enter a valid email address.' } } } });
    const auth = createEmailAuth();
    await expect(auth.login('   ')).rejects.toThrow('Enter your email address.');
    expect(fetch).not.toHaveBeenCalled();
    render(<App config={base} />);
    await userEvent.type(await screen.findByLabelText('Email'), 'not-an-email@x');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('Enter a valid email address.')).toBeInTheDocument();
  });

  it('an email that has not signed up is sent to Sign up, which registers and signs in', async () => {
    const fetch = mockFetch({
      '/api/v1/auth/email-login': { status: 404, body: { error: { code: 'USER_NOT_FOUND', message: 'No account uses this email. Sign up first.' } } },
      '/api/v1/auth/email-signup': { ...login, status: 201 },
      '/api/v1/me': me(false),
      '/api/v1/questions/today': none,
    });
    render(<App config={base} />);
    await userEvent.type(await screen.findByLabelText('Email'), 'first.last@example.test');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('No account uses this email. Sign up first.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Sign up to answer daily questions' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Sign up' }));
    expect(await screen.findByText('first.last@example.test')).toBeInTheDocument();
    expect(fetch.mock.calls.some(([u]) => u === '/api/v1/auth/email-signup')).toBe(true);
  });

  it('signing up with a registered email is sent back to Sign in', async () => {
    mockFetch({ '/api/v1/auth/email-signup': { status: 409, body: { error: { code: 'USER_EXISTS', message: 'An account already uses this email. Sign in instead.' } } } });
    render(<App config={base} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sign up' }));
    await userEvent.type(screen.getByLabelText('Email'), 'first.last@example.test');
    await userEvent.click(screen.getByRole('button', { name: 'Sign up' }));
    expect(await screen.findByText('An account already uses this email. Sign in instead.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: "Sign in to answer today's question" })).toBeInTheDocument();
  });

  it('logout clears the stored session and the email field', async () => {
    mockFetch({ '/api/v1/auth/email-login': login, '/api/v1/me': me(false), '/api/v1/questions/today': none });
    render(<App config={base} />);
    await userEvent.type(await screen.findByLabelText('Email'), 'first.last@example.test');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByText('first.last@example.test');
    expect(sessionStorage.length).toBe(1);
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(await screen.findByLabelText('Email')).toHaveValue('');
    expect(sessionStorage.length).toBe(0);
    await expect(createEmailAuth().init()).resolves.toBe(false);
  });

  it('Entra mode offers only Microsoft sign-in, and a failure never falls back to email', async () => {
    const fetch = mockFetch({});
    render(<App config={{ ...base, authMode: 'entra' }} auth={fakeEntra(true)} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sign in with Microsoft' }));
    expect(await screen.findByText('Microsoft sign-in failed.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
    expect(fetch.mock.calls.some(([u]) => String(u).includes('email-login'))).toBe(false);
  });

  it('shows Admin navigation for Entra administrators', async () => {
    mockFetch({ '/api/v1/me': me(true, 'entra'), '/api/v1/questions/today': none });
    render(<App config={{ ...base, authMode: 'entra' }} auth={fakeEntra()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sign in with Microsoft' }));
    expect(await screen.findByRole('button', { name: 'Admin' })).toBeInTheDocument();
  });

  it('open test mode shows a notice and never offers sign-in', async () => {
    const fetch = mockFetch({});
    render(<App config={{ ...base, authMode: 'open' }} />);
    expect(screen.getByRole('heading', { name: 'Open test mode' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument();
    expect(screen.queryByText('Sign in with Microsoft')).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it('explains forbidden access', async () => {
    mockFetch({ '/api/v1/me': { status: 403, body: { error: { code: 'GUEST_NOT_ALLOWED', message: 'Guest accounts cannot use this application.' } } } });
    render(<App config={{ ...base, authMode: 'entra' }} auth={fakeEntra()} />);
    await userEvent.click(await screen.findByRole('button', { name: 'Sign in with Microsoft' }));
    expect(await screen.findByText('Guest accounts cannot use this application.')).toBeInTheDocument();
  });
});
