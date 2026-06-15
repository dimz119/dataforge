import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router';

import { mapValidationProblem } from '../../../shared/api/formErrors';
import { Button, FormField, Input, useToast } from '../../../shared/ui';
import { useResetPassword } from '../api';
import { resetSchema, type ResetValues } from '../schemas';

/**
 * Reset-password confirm (frontend-architecture §9.1). The token is the
 * credential (no guard). The backend emails a PATH-param link
 * (`/reset-password/{token}`, identity/infra/email.py); the `?token=` query form
 * is also accepted. Success → login. Invalid/expired token surfaces on the form
 * banner (INV-ID-3).
 */
export function ResetPasswordPage() {
  const [params] = useSearchParams();
  const { token: pathToken } = useParams();
  const token = pathToken ?? params.get('token');
  const reset = useResetPassword();
  const navigate = useNavigate();
  const toast = useToast();
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<ResetValues>({ resolver: zodResolver(resetSchema) });

  if (!token) {
    return (
      <div className="flex flex-col gap-4 text-center">
        <h1 className="text-lg font-semibold text-text">Invalid reset link</h1>
        <p className="text-sm text-text-muted">This link is missing its token.</p>
        <Link to="/forgot-password" className="text-sm text-accent hover:underline">
          Request a new link
        </Link>
      </div>
    );
  }

  const onSubmit = handleSubmit((values) => {
    reset.mutate(
      { token, new_password: values.password },
      {
        onSuccess: () => {
          toast.show({ title: 'Password updated', tone: 'success' });
          void navigate('/login', { replace: true });
        },
        onError: (err) => {
          const mapped = mapValidationProblem(err, ['new_password', 'token']);
          if (mapped.fields.new_password)
            setError('password', { message: mapped.fields.new_password });
          const banner = [...mapped.formLevel, mapped.fields.token].filter(Boolean).join(' ');
          if (banner) setError('root', { message: banner });
        },
      },
    );
  });

  return (
    <form
      onSubmit={(e) => {
        void onSubmit(e);
      }}
      noValidate
      className="flex flex-col gap-4"
    >
      <h1 className="text-lg font-semibold text-text">Choose a new password</h1>
      {errors.root && (
        <p role="alert" className="rounded-md bg-danger/10 px-3 py-2 text-sm text-danger">
          {errors.root.message}
        </p>
      )}
      <FormField label="New password" error={errors.password?.message} hint="At least 10 characters.">
        {(p) => (
          <Input type="password" autoComplete="new-password" {...p} {...register('password')} />
        )}
      </FormField>
      <FormField label="Confirm password" error={errors.confirm?.message}>
        {(p) => (
          <Input type="password" autoComplete="new-password" {...p} {...register('confirm')} />
        )}
      </FormField>
      <Button type="submit" loading={reset.isPending}>
        Update password
      </Button>
    </form>
  );
}
