import type { RouteObject } from 'react-router';

import { CheckEmailPage } from './pages/CheckEmailPage';
import { ForgotPasswordPage } from './pages/ForgotPasswordPage';
import { LoginPage } from './pages/LoginPage';
import { ResetPasswordPage } from './pages/ResetPasswordPage';
import { SignupPage } from './pages/SignupPage';
import { VerifyEmailPage } from './pages/VerifyEmailPage';

/** Auth routes mounted behind the PublicOnly guard by app/router.tsx (§3.1). */
export const authPublicOnlyRoutes: RouteObject[] = [
  { path: '/login', element: <LoginPage /> },
  { path: '/signup', element: <SignupPage /> },
  { path: '/signup/check-email', element: <CheckEmailPage /> },
  { path: '/forgot-password', element: <ForgotPasswordPage /> },
];

/**
 * Token-credentialed auth routes — no guard (the token is the credential).
 *
 * The backend emails a PATH-param link (`/verify-email/{token}`,
 * `/reset-password/{token}` — identity/infra/email.py); we also accept the
 * `?token=` query form for resends/manual entry. Both shapes mount the same page,
 * which reads the path param first, then the query (VerifyEmailPage/ResetPasswordPage).
 */
export const authTokenRoutes: RouteObject[] = [
  { path: '/verify-email', element: <VerifyEmailPage /> },
  { path: '/verify-email/:token', element: <VerifyEmailPage /> },
  { path: '/reset-password', element: <ResetPasswordPage /> },
  { path: '/reset-password/:token', element: <ResetPasswordPage /> },
];
