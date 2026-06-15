import { zodResolver } from '@hookform/resolvers/zod';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link } from 'react-router';

import { Button, FormField, Input } from '../../../shared/ui';
import { useForgotPassword } from '../api';
import { emailSchema, type EmailValues } from '../schemas';

/**
 * Forgot-password request (frontend-architecture §9.1). The response NEVER
 * reveals whether the email exists (anti-enumeration; same contract as the API):
 * any submit shows the same confirmation.
 */
export function ForgotPasswordPage() {
  const forgot = useForgotPassword();
  const [submitted, setSubmitted] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<EmailValues>({ resolver: zodResolver(emailSchema) });

  const onSubmit = handleSubmit((values) => {
    // Show the same outcome regardless of result (anti-enumeration).
    forgot.mutate(values, { onSettled: () => setSubmitted(true) });
  });

  if (submitted) {
    return (
      <div className="flex flex-col gap-4 text-center">
        <h1 className="text-lg font-semibold text-text">Check your email</h1>
        <p className="text-sm text-text-muted">
          If an account exists for that address, we sent a password-reset link.
        </p>
        <Link to="/login" className="text-sm text-accent hover:underline">
          Back to log in
        </Link>
      </div>
    );
  }

  return (
    <form
      onSubmit={(e) => {
        void onSubmit(e);
      }}
      noValidate
      className="flex flex-col gap-4"
    >
      <h1 className="text-lg font-semibold text-text">Reset your password</h1>
      <p className="text-sm text-text-muted">We&apos;ll email you a reset link.</p>
      <FormField label="Email" error={errors.email?.message}>
        {(p) => <Input type="email" autoComplete="email" {...p} {...register('email')} />}
      </FormField>
      <Button type="submit" loading={forgot.isPending}>
        Send reset link
      </Button>
      <Link to="/login" className="text-sm text-accent hover:underline">
        Back to log in
      </Link>
    </form>
  );
}
