/**
 * Auth feature data layer (frontend-architecture §6). queryOptions + mutation
 * hooks for the session + the auth pages. The transport (`api`), TokenManager,
 * query keys, and invalidation matrix all come from `shared/api` (IMP-1).
 */
import { queryOptions, useMutation, useQueryClient } from '@tanstack/react-query';

import { api, tokenManager } from '../../shared/api/client';
import { invalidate } from '../../shared/api/invalidation';
import { ApiError } from '../../shared/api/problem';
import { queryKeys, staleTimes } from '../../shared/api/queryKeys';
import type {
  EmailOnlyRequest,
  LoginRequest,
  PasswordResetConfirmRequest,
  SignupRequest,
  SignupResponse,
  UserMeResponse,
  VerifyEmailRequest,
  VerifyEmailResponse,
} from '../../shared/api/types';

/** `['session']` → current user + memberships (§4.2). 5-min staleTime. */
export function sessionQueryOptions() {
  return queryOptions({
    queryKey: queryKeys.session(),
    staleTime: staleTimes.session,
    retry: false,
    queryFn: async (): Promise<UserMeResponse> => {
      const { data, error } = await api.GET('/api/v1/users/me');
      if (error) throw error as ApiError;
      return data;
    },
  });
}

/**
 * Session bootstrap (§6.2): refresh via the df_refresh cookie to obtain an access
 * token, then read /users/me. Called by RequireAuth before guarded content
 * renders. Returns null when unauthenticated (no/expired cookie → 401).
 */
export async function bootstrapSession(): Promise<UserMeResponse | null> {
  try {
    await tokenManager.refresh();
  } catch {
    return null; // no valid refresh cookie → unauthenticated
  }
  const { data, error } = await api.GET('/api/v1/users/me');
  if (error) return null;
  return data;
}

export function useLogin() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: LoginRequest) => {
      const { data, error } = await api.POST('/api/v1/auth/login', { body });
      if (error) throw error as ApiError;
      tokenManager.setAccess(data, 'login');
      return data;
    },
    onSuccess: async () => {
      // Seed ['session'] from the just-acquired token (§4.3 login row).
      await qc.invalidateQueries({ queryKey: queryKeys.session() });
    },
  });
}

export function useLogout() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      // Best-effort: blacklist server-side; ignore failure so logout never wedges.
      await api.POST('/api/v1/auth/logout', {}).catch(() => undefined);
      tokenManager.broadcastLogout();
    },
    onSuccess: () => invalidate.onLogout(qc),
  });
}

export function useSignup() {
  return useMutation({
    mutationFn: async (body: SignupRequest): Promise<SignupResponse> => {
      const { data, error } = await api.POST('/api/v1/auth/signup', { body });
      if (error) throw error as ApiError;
      return data;
    },
  });
}

export function useVerifyEmail() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: VerifyEmailRequest): Promise<VerifyEmailResponse> => {
      const { data, error } = await api.POST('/api/v1/auth/verify-email', { body });
      if (error) throw error as ApiError;
      return data;
    },
    onSuccess: () => invalidate.onSession(qc),
  });
}

/**
 * Single-use email-token consumption, deduplicated by token (§9.1). The token is
 * single-use: React 18 StrictMode (dev) mounts the verify page twice, so the
 * effect can fire twice for the same token. We cache the in-flight promise per
 * token so both StrictMode passes (and any re-render) share ONE network call —
 * the second pass never re-POSTs an already-consumed token (which the backend
 * would reject). The surviving (second) StrictMode instance awaits the same
 * shared promise and resolves its own state, which the discarded inline-callback
 * pattern could not do (callbacks on the unmounted first instance are dropped).
 */
const verifyEmailInFlight = new Map<string, Promise<VerifyEmailResponse>>();

export async function verifyEmailOnce(token: string): Promise<VerifyEmailResponse> {
  const existing = verifyEmailInFlight.get(token);
  if (existing) return existing;
  const promise = (async (): Promise<VerifyEmailResponse> => {
    const { data, error } = await api.POST('/api/v1/auth/verify-email', {
      body: { token },
    });
    if (error) throw error as ApiError;
    return data;
  })();
  verifyEmailInFlight.set(token, promise);
  return promise;
}

export function useResendVerification() {
  return useMutation({
    mutationFn: async (body: EmailOnlyRequest) => {
      const { error } = await api.POST('/api/v1/auth/resend-verification', { body });
      if (error) throw error as ApiError;
    },
  });
}

export function useForgotPassword() {
  return useMutation({
    mutationFn: async (body: EmailOnlyRequest) => {
      const { error } = await api.POST('/api/v1/auth/password-reset', { body });
      if (error) throw error as ApiError;
    },
  });
}

export function useResetPassword() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: PasswordResetConfirmRequest) => {
      const { error } = await api.POST('/api/v1/auth/password-reset/confirm', { body });
      if (error) throw error as ApiError;
    },
    onSuccess: () => invalidate.onSession(qc),
  });
}
