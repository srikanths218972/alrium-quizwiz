export interface RuntimeConfig {
  appName: string;
  authMode: 'entra' | 'email' | 'open';
  tenantId: string;
  clientId: string;
  apiScope: string;
  redirectUri: string;
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  const res = await fetch('/config.json', { cache: 'no-store' });
  if (!res.ok) throw new Error('Could not load application configuration.');
  const c = (await res.json()) as Partial<RuntimeConfig>;
  const modeRes = await fetch('/api/v1/auth/mode', { cache: 'no-store' });
  if (!modeRes.ok) throw new Error('Could not load application configuration.');
  const mode = (await modeRes.json())?.data?.mode;
  if (mode !== 'entra' && mode !== 'email' && mode !== 'open') throw new Error('Could not load application configuration.');
  return {
    appName: c.appName || 'ålrium - QuizWiz',
    authMode: mode,
    tenantId: c.tenantId ?? '',
    clientId: c.clientId ?? '',
    apiScope: c.apiScope ?? '',
    redirectUri: c.redirectUri || window.location.origin + '/',
  };
}

export const LIMITS = { question: 500, option: 200 } as const;
