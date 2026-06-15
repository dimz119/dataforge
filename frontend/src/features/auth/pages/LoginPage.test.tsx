import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../../../shared/api/problem';
import { renderWithProviders } from '../../../shared/testing/renderWithProviders';
import { LoginPage } from './LoginPage';

// Mock the auth data layer so the page renders without a live transport.
const login = vi.fn();
vi.mock('../api', () => ({
  useLogin: () => ({ mutate: login, isPending: false }),
}));

function renderLogin() {
  return renderWithProviders(
    <MemoryRouter initialEntries={['/login']}>
      <LoginPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  login.mockReset();
});

describe('LoginPage', () => {
  it('renders the email + password fields and submit', () => {
    renderLogin();
    expect(screen.getByRole('heading', { name: 'Log in' })).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Log in' })).toBeInTheDocument();
  });

  it('shows a client-side validation error for an invalid email', async () => {
    const user = userEvent.setup();
    renderLogin();
    await user.type(screen.getByLabelText('Email'), 'not-an-email');
    await user.type(screen.getByLabelText('Password'), 'secret123');
    await user.click(screen.getByRole('button', { name: 'Log in' }));
    expect(await screen.findByText('Enter a valid email.')).toBeInTheDocument();
    expect(login).not.toHaveBeenCalled();
  });

  it('submits valid credentials to the login mutation', async () => {
    const user = userEvent.setup();
    renderLogin();
    await user.type(screen.getByLabelText('Email'), 'rosa@example.net');
    await user.type(screen.getByLabelText('Password'), 'correct horse');
    await user.click(screen.getByRole('button', { name: 'Log in' }));
    await waitFor(() =>
      expect(login).toHaveBeenCalledWith(
        { email: 'rosa@example.net', password: 'correct horse' },
        expect.any(Object),
      ),
    );
  });

  it('surfaces an authentication-failed problem on the form banner', async () => {
    const user = userEvent.setup();
    login.mockImplementation((_vals, opts: { onError: (e: unknown) => void }) => {
      opts.onError(
        new ApiError({
          status: 401,
          type: 'https://docs.dataforge.dev/problems/authentication-failed',
          title: 'Invalid credentials',
          detail: 'Bad email or password.',
        }),
      );
    });
    renderLogin();
    await user.type(screen.getByLabelText('Email'), 'rosa@example.net');
    await user.type(screen.getByLabelText('Password'), 'wrongpass12');
    await user.click(screen.getByRole('button', { name: 'Log in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Bad email or password.');
  });
});
