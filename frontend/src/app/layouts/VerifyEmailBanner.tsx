import { useState } from 'react';

import { useResendVerification } from '../../features/auth/api';
import { Button, useToast } from '../../shared/ui';

/**
 * Persistent verify-email banner (frontend-architecture §3.2). Unverified users
 * are NOT route-blocked; they see this banner with a re-send link. The API
 * enforces the same rule on tenant-creating actions — this is a UX mirror.
 */
export function VerifyEmailBanner({ email }: { email: string }) {
  const resend = useResendVerification();
  const toast = useToast();
  const [sent, setSent] = useState(false);

  const onResend = () => {
    resend.mutate(
      { email },
      {
        onSuccess: () => {
          setSent(true);
          toast.show({ title: 'Verification email sent', tone: 'success' });
        },
        onError: (err) => toast.showError(err, 'Could not send verification email'),
      },
    );
  };

  return (
    <div
      role="region"
      aria-label="Email verification"
      className="flex items-center justify-between gap-4 border-b border-warning/40 bg-warning/10 px-5 py-2 text-sm text-text"
    >
      <span>
        Verify your email to create workspaces, API keys, and streams. We sent a link to{' '}
        <strong>{email}</strong>.
      </span>
      <Button variant="secondary" size="sm" onClick={onResend} loading={resend.isPending} disabled={sent}>
        {sent ? 'Sent' : 'Resend'}
      </Button>
    </div>
  );
}
