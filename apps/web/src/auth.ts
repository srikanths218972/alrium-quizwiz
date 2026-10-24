import { InteractionRequiredAuthError, PublicClientApplication, type AccountInfo } from '@azure/msal-browser';
import type { RuntimeConfig } from './config';

export interface AuthClient {
  mode: 'entra' | 'email' | 'open';
  init(): Promise<boolean>;
  login(email?: string): Promise<boolean>;
  signup?(email: string): Promise<boolean>;
  logout(): Promise<void>;
  headers(): Promise<Record<string, string>>;
}

export function createEntraAuth(cfg: RuntimeConfig): AuthClient {
  const pca = new PublicClientApplication({
    auth: { clientId: cfg.clientId, authority: `https://login.microsoftonline.com/${cfg.tenantId}`, redirectUri: cfg.redirectUri },
    cache: { cacheLocation: 'sessionStorage' },
  });
  const scopes = [cfg.apiScope];
  let account: AccountInfo | null = null;

  return {
    mode: 'entra',
    async init() {
      await pca.initialize();
      const result = await pca.handleRedirectPromise();
      account = result?.account ?? pca.getAllAccounts()[0] ?? null;
      return account !== null;
    },
    async login() {
      await pca.loginRedirect({ scopes });
      return false;
    },
    async logout() {
      await pca.logoutRedirect({ account: account ?? undefined });
    },
    async headers() {
      if (!account) throw new Error('Not signed in');
      try {
        return { authorization: `Bearer ${(await pca.acquireTokenSilent({ scopes, account })).accessToken}` };
      } catch (err) {
        if (err instanceof InteractionRequiredAuthError) {
          await pca.acquireTokenRedirect({ scopes, account });
        }
        throw err;
      }
    },
  };
}

export class SignInError extends Error {
  constructor(message: string, public code = '') {
    super(message);
  }
}

export function createEmailAuth(): AuthClient {
  const KEY = 'quizwiz-email-session';
  let session: { token: string; expires: number } | null = null;

  async function start(path: string, email: string) {
    if (!email.trim()) throw new SignInError('Enter your email address.');
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ email }),
      cache: 'no-store',
    });
    const json = await res.json().catch(() => null);
    if (!res.ok) throw new SignInError(json?.error?.message ?? 'Sign-in failed.', json?.error?.code ?? '');
    session = { token: json.data.access_token, expires: Date.now() + (json.data.expires_in - 30) * 1000 };
    sessionStorage.setItem(KEY, JSON.stringify(session));
    return true;
  }

  return {
    mode: 'email',
    async init() {
      try {
        session = JSON.parse(sessionStorage.getItem(KEY) ?? 'null');
      } catch {
        session = null;
      }
      if (session && Date.now() > session.expires) session = null;
      return session !== null;
    },
    async login(email = '') {
      return start('/api/v1/auth/email-login', email);
    },
    async signup(email: string) {
      return start('/api/v1/auth/email-signup', email);
    },
    async logout() {
      session = null;
      sessionStorage.removeItem(KEY);
    },
    async headers() {
      if (!session) throw new Error('Not signed in');
      return { authorization: `Bearer ${session.token}` };
    },
  };
}
