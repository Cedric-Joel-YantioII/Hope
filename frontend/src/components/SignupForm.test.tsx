import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { SignupForm } from './SignupForm';

describe('SignupForm', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('POSTs JSON {email, password, name} to /signup and reports success', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ user_id: 'u_123', email: 'a@b.co' }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    const onSuccess = vi.fn();
    render(<SignupForm onSuccess={onSuccess} />);

    fireEvent.change(screen.getByLabelText(/name/i), {
      target: { value: 'Ada Lovelace' },
    });
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: 'a@b.co' },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'hunter2hunter2' },
    });
    fireEvent.submit(screen.getByRole('form', { name: /signup/i }));

    await screen.findByRole('status');

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe('http://localhost:8000/signup');
    expect(init?.method).toBe('POST');
    expect(
      (init?.headers as Record<string, string>)['Content-Type'],
    ).toBe('application/json');
    expect(JSON.parse(init?.body as string)).toEqual({
      email: 'a@b.co',
      password: 'hunter2hunter2',
      name: 'Ada Lovelace',
    });
    expect(onSuccess).toHaveBeenCalledWith({
      user_id: 'u_123',
      email: 'a@b.co',
    });
  });

  it('surfaces a 409 email_taken error against the email field', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ error: 'email_taken' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    render(<SignupForm />);
    fireEvent.change(screen.getByLabelText(/name/i), {
      target: { value: 'Ada' },
    });
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: 'taken@b.co' },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'hunter2hunter2' },
    });
    fireEvent.submit(screen.getByRole('form', { name: /signup/i }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('email_taken');
    expect(alert.getAttribute('data-field')).toBe('email');
  });

  it('surfaces a 400 validation error against the named field', async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ error: 'too_short', field: 'password' }),
        { status: 400, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    render(<SignupForm />);
    fireEvent.change(screen.getByLabelText(/name/i), {
      target: { value: 'Ada' },
    });
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: 'a@b.co' },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: 'hunter2hunter2' },
    });
    fireEvent.submit(screen.getByRole('form', { name: /signup/i }));

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toBe('too_short');
    expect(alert.getAttribute('data-field')).toBe('password');
  });
});
