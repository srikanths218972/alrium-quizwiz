import { useEffect, useMemo, useState } from 'react';
import { ApiError, createApi, type Me } from './api';
import { createEmailAuth, createEntraAuth, SignInError, type AuthClient } from './auth';
import { Admin } from './components/Admin';
import { Quiz } from './components/Quiz';
import type { RuntimeConfig } from './config';
import { formatBusinessDate } from './format';

type Phase = 'starting' | 'signed-out' | 'signed-in' | 'denied' | 'unavailable';

export function App({ config, auth: injected }: { config: RuntimeConfig; auth?: AuthClient }) {
  const auth = useMemo(() => injected ?? (config.authMode === 'entra' ? createEntraAuth(config) : createEmailAuth()), [config, injected]);
  const api = useMemo(() => createApi(() => auth.headers()), [auth]);
  const [phase, setPhase] = useState<Phase>('starting');
  const [me, setMe] = useState<Me | null>(null);
  const [view, setView] = useState<'quiz' | 'admin'>('quiz');
  const [problem, setProblem] = useState('');
  const [email, setEmail] = useState('');
  const [form, setForm] = useState<'signin' | 'signup'>('signin');

  useEffect(() => {
    document.title = config.appName;
  }, [config.appName]);

  async function loadMe() {
    try {
      setMe(await api.me());
      setPhase('signed-in');
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        await auth.logout().catch(() => undefined);
        setPhase('signed-out');
      } else if (err instanceof ApiError && err.status === 403) {
        setProblem(err.message);
        setPhase('denied');
      } else {
        setProblem(err instanceof ApiError ? err.message : 'Sign-in failed.');
        setPhase('unavailable');
      }
    }
  }

  useEffect(() => {
    auth.init().then(
      (signedIn) => (signedIn ? loadMe() : setPhase('signed-out')),
      () => {
        setProblem('Sign-in could not be started. Check the application configuration.');
        setPhase('unavailable');
      },
    );
  }, [auth]);

  async function signIn() {
    setProblem('');
    try {
      const ok = form === 'signup' && auth.signup ? await auth.signup(email) : await auth.login(email);
      if (ok) await loadMe();
    } catch (err) {
      if (err instanceof SignInError && err.code === 'USER_NOT_FOUND') setForm('signup');
      if (err instanceof SignInError && err.code === 'USER_EXISTS') setForm('signin');
      setProblem(err instanceof Error ? err.message : 'Sign-in failed.');
    }
  }

  function switchForm(next: 'signin' | 'signup') {
    setForm(next);
    setProblem('');
  }

  async function expired() {
    await auth.logout().catch(() => undefined);
    setMe(null);
    setView('quiz');
    setProblem('Your session has expired. Please sign in again.');
    setPhase('signed-out');
  }

  async function signOut() {
    await auth.logout();
    setEmail('');
    setMe(null);
    setView('quiz');
    setPhase('signed-out');
  }

  if (config.authMode === 'open') {
    return (
      <div className="app">
        <header><h1>{config.appName}</h1></header>
        <main>
          <section className="card" role="note">
            <h2>Open test mode</h2>
            <p>This server is running without sign-in for API testing. Use the Postman collection; the web app is not available in this mode.</p>
          </section>
        </main>
      </div>
    );
  }

  return (
    <div className="app">
      <header>
        <h1>{config.appName}</h1>
        {me && (
          <div className="who">
            <span>{me.user_name}</span>
            {me.is_admin && (
              <nav aria-label="Views">
                <button type="button" aria-pressed={view === 'quiz'} onClick={() => setView('quiz')}>Today</button>
                <button type="button" aria-pressed={view === 'admin'} onClick={() => setView('admin')}>Admin</button>
              </nav>
            )}
            <button type="button" onClick={() => void signOut()}>Sign out</button>
          </div>
        )}
      </header>
      {me && <p className="today">Today in Delhi: {formatBusinessDate(me.business_date)}</p>}

      <main>
        {phase === 'starting' && <p className="status" role="status">Loading…</p>}

        {phase === 'signed-out' && (
          <section className="card signin">
            <h2>{auth.mode === 'email' && form === 'signup' ? 'Sign up to answer daily questions' : "Sign in to answer today's question"}</h2>
            {auth.mode === 'email' ? (
              <form onSubmit={(e) => { e.preventDefault(); void signIn(); }}>
                <p>{form === 'signup' ? 'Register with your email. No password is needed.' : 'Sign in with the email you signed up with.'} Email ownership is not verified.</p>
                <label>
                  Email
                  <input type="email" autoComplete="email" maxLength={200} required value={email} onChange={(e) => setEmail(e.target.value)} />
                </label>
                <button type="submit" className="primary">{form === 'signup' ? 'Sign up' : 'Sign in'}</button>
                <p className="switch">
                  {form === 'signup' ? 'Already signed up? ' : 'New here? '}
                  <button type="button" className="link" onClick={() => switchForm(form === 'signup' ? 'signin' : 'signup')}>
                    {form === 'signup' ? 'Sign in' : 'Sign up'}
                  </button>
                </p>
              </form>
            ) : (
              <button type="button" className="primary" onClick={() => void signIn()}>Sign in with Microsoft</button>
            )}
            {problem && <p className="error" role="alert">{problem}</p>}
          </section>
        )}

        {phase === 'denied' && (
          <section className="card" role="alert">
            <h2>Access not available</h2>
            <p>{problem}</p>
            <button type="button" onClick={() => void signOut()}>Sign out</button>
          </section>
        )}

        {phase === 'unavailable' && (
          <section className="card" role="alert">
            <p>{problem || 'The service is unavailable.'}</p>
            <button type="button" onClick={() => window.location.reload()}>Reload</button>
          </section>
        )}

        {phase === 'signed-in' && me && (view === 'admin' && me.is_admin ? <Admin api={api} today={me.business_date} /> : <Quiz api={api} onExpired={() => void expired()} />)}
      </main>
    </div>
  );
}
