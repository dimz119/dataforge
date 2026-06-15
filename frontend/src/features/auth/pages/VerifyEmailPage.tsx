import { zodResolver } from '@hookform/resolvers/zod';
import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link, useParams, useSearchParams } from 'react-router';

import { Button, FormField, Input } from '../../../shared/ui';
import { useResendVerification, verifyEmailOnce } from '../api';
import { emailSchema, type EmailValues } from '../schemas';

type Phase = 'pending' | 'success' | 'failure' | 'missing';

/**
 * Verify-email page (frontend-architecture §9.1). The token is the credential
 * (no guard). The backend emails a PATH-param link (`/verify-email/{token}`,
 * identity/infra/email.py); the `?token=` query form is also accepted. pending →
 * success ("go to console") or failure (expired/used token, INV-ID-3) with a
 * re-send form.
 *
 * The token consumption goes through `verifyEmailOnce`, which deduplicates the
 * single-use POST by token. Under React 18 StrictMode (dev) the page mounts
 * twice; both passes call `verifyEmailOnce` but share ONE in-flight promise, and
 * the surviving instance resolves its own state from that shared promise (a
 * `mounted` guard prevents a setState on the discarded first instance). Inline
 * `mutate` callbacks could not do this — they are dropped when StrictMode
 * unmounts the first instance before the mutation settles.
 */
export function VerifyEmailPage() {
  const [params] = useSearchParams();
  const { token: pathToken } = useParams();
  const token = pathToken ?? params.get('token');
  const [phase, setPhase] = useState<Phase>(token ? 'pending' : 'missing');

  useEffect(() => {
    if (!token) {
      setPhase('missing');
      return;
    }
    let mounted = true;
    setPhase('pending');
    verifyEmailOnce(token).then(
      () => {
        if (mounted) setPhase('success');
      },
      () => {
        if (mounted) setPhase('failure');
      },
    );
    return () => {
      mounted = false;
    };
  }, [token]);

  if (phase === 'success') {
    return (
      <div className="flex flex-col gap-4 text-center">
        <h1 className="text-lg font-semibold text-text">Email verified</h1>
        <p className="text-sm text-text-muted">Your account is active.</p>
        <Link
          to="/"
          className="inline-flex h-10 items-center justify-center rounded-md bg-accent px-4 text-sm font-medium text-accent-fg hover:bg-accent-hover"
        >
          Go to console
        </Link>
      </div>
    );
  }

  if (phase === 'pending') {
    return (
      <div className="flex flex-col gap-3 text-center">
        <h1 className="text-lg font-semibold text-text">Verifying…</h1>
        <p className="text-sm text-text-muted">Confirming your verification link.</p>
      </div>
    );
  }

  // failure or missing token: offer a resend.
  return <ResendForm />;
}

function ResendForm() {
  const resend = useResendVerification();
  const [sent, setSent] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<EmailValues>({ resolver: zodResolver(emailSchema) });

  const onSubmit = handleSubmit((values) => {
    resend.mutate(values, { onSuccess: () => setSent(true) });
  });

  return (
    <form
      onSubmit={(e) => {
        void onSubmit(e);
      }}
      noValidate
      className="flex flex-col gap-4"
    >
      <h1 className="text-lg font-semibold text-text">Link expired or invalid</h1>
      <p className="text-sm text-text-muted">
        Verification links are single-use and expire after 24 hours. Request a new one.
      </p>
      <FormField label="Email" error={errors.email?.message}>
        {(p) => <Input type="email" autoComplete="email" {...p} {...register('email')} />}
      </FormField>
      <Button type="submit" loading={resend.isPending} disabled={sent}>
        {sent ? 'Email sent' : 'Send new link'}
      </Button>
      <Link to="/login" className="text-sm text-accent hover:underline">
        Back to log in
      </Link>
    </form>
  );
}
