import { useState, type FormEvent } from 'react';

export interface SignupFormProps {
  endpoint?: string;
  onSuccess?: (result: { user_id: string; email: string }) => void;
}

type Status =
  | { kind: 'idle' }
  | { kind: 'submitting' }
  | { kind: 'error'; message: string; field?: 'email' | 'password' | 'name' }
  | { kind: 'success'; user_id: string };

export function SignupForm({
  endpoint = 'http://localhost:8000/signup',
  onSuccess,
}: SignupFormProps) {
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [status, setStatus] = useState<Status>({ kind: 'idle' });

  async function handleSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setStatus({ kind: 'submitting' });

    let res: Response;
    try {
      res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, name }),
      });
    } catch {
      setStatus({ kind: 'error', message: 'network_error' });
      return;
    }

    if (res.status === 201) {
      const data = (await res.json()) as { user_id: string; email: string };
      setStatus({ kind: 'success', user_id: data.user_id });
      onSuccess?.(data);
      return;
    }

    if (res.status === 400) {
      const data = (await res.json()) as {
        error: string;
        field?: 'email' | 'password' | 'name';
      };
      setStatus({ kind: 'error', message: data.error, field: data.field });
      return;
    }

    if (res.status === 409) {
      const data = (await res.json()) as { error: string };
      setStatus({ kind: 'error', message: data.error, field: 'email' });
      return;
    }

    setStatus({ kind: 'error', message: `unexpected_status_${res.status}` });
  }

  return (
    <form onSubmit={handleSubmit} aria-label="signup">
      <label>
        Name
        <input
          type="text"
          name="name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />
      </label>
      <label>
        Email
        <input
          type="email"
          name="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
      </label>
      <label>
        Password
        <input
          type="password"
          name="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          minLength={8}
          required
        />
      </label>
      <button type="submit" disabled={status.kind === 'submitting'}>
        {status.kind === 'submitting' ? 'Creating account…' : 'Sign up'}
      </button>
      {status.kind === 'error' && (
        <p role="alert" data-field={status.field ?? ''}>
          {status.message}
        </p>
      )}
      {status.kind === 'success' && (
        <p role="status">Account created (id: {status.user_id}).</p>
      )}
    </form>
  );
}
