import { useState } from 'react';
import { Link, useLocation } from 'react-router';

import { Button, useToast } from '../../../shared/ui';
import { useResendVerification } from '../api';

/**
 * Post-signup confirmation (frontend-architecture §9.1). Tells the user to check
 * their inbox and offers a resend. The email arrives via signup-page nav state.
 */
export function CheckEmailPage() {
  const location = useLocation();
  const email = (location.state as { email?: string } | null)?.email;
  const resend = useResendVerification();
  const toast = useToast();
  const [sent, setSent] = useState(false);

  const onResend = () => {
    if (!email) return;
    resend.mutate(
      { email },
      {
        onSuccess: () => {
          setSent(true);
          toast.show({ title: 'Verification email sent', tone: 'success' });
        },
        onError: (err) => toast.showError(err, 'Could not resend'),
      },
    );
  };

  return (
    <div className="flex flex-col gap-4 text-center">
      <h1 className="text-lg font-semibold text-text">Check your email</h1>
      <p className="text-sm text-text-muted">
        We sent a verification link{email ? <> to <strong>{email}</strong></> : ''}. Open it to
        activate your account.
      </p>
      {email && (
        <Button variant="secondary" onClick={onResend} loading={resend.isPending} disabled={sent}>
          {sent ? 'Sent' : 'Resend email'}
        </Button>
      )}
      <Link to="/login" className="text-sm text-accent hover:underline">
        Back to log in
      </Link>
    </div>
  );
}
