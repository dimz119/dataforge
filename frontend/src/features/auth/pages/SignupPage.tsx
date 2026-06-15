import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { Link, useNavigate } from 'react-router';

import { mapValidationProblem } from '../../../shared/api/formErrors';
import { Button, FormField, Input } from '../../../shared/ui';
import { useSignup } from '../api';
import { signupSchema, type SignupValues } from '../schemas';

/**
 * Signup page (frontend-architecture §9.1 SignupForm). Email + password + confirm
 * with a zod policy mirror; duplicate email → conflict surfaced to the email
 * field; success → /signup/check-email.
 */
export function SignupPage() {
  const signup = useSignup();
  const navigate = useNavigate();
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<SignupValues>({ resolver: zodResolver(signupSchema) });

  const onSubmit = handleSubmit((values) => {
    signup.mutate(
      { email: values.email, password: values.password },
      {
        onSuccess: () => {
          void navigate('/signup/check-email', { state: { email: values.email } });
        },
        onError: (err) => {
          // 409 conflict (duplicate email) has no errors[]; surface on the email field.
          const mapped = mapValidationProblem(err, ['email', 'password']);
          if (mapped.fields.email) setError('email', { message: mapped.fields.email });
          if (mapped.fields.password) setError('password', { message: mapped.fields.password });
          if (mapped.formLevel.length > 0) setError('root', { message: mapped.formLevel.join(' ') });
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
      <h1 className="text-lg font-semibold text-text">Create your account</h1>
      {errors.root && (
        <p role="alert" className="rounded-md bg-danger/10 px-3 py-2 text-sm text-danger">
          {errors.root.message}
        </p>
      )}
      <FormField label="Email" error={errors.email?.message}>
        {(p) => <Input type="email" autoComplete="email" {...p} {...register('email')} />}
      </FormField>
      <FormField label="Password" error={errors.password?.message} hint="At least 10 characters.">
        {(p) => (
          <Input type="password" autoComplete="new-password" {...p} {...register('password')} />
        )}
      </FormField>
      <FormField label="Confirm password" error={errors.confirm?.message}>
        {(p) => (
          <Input type="password" autoComplete="new-password" {...p} {...register('confirm')} />
        )}
      </FormField>
      <Button type="submit" loading={signup.isPending}>
        Create account
      </Button>
      <p className="text-sm text-text-muted">
        Already have an account?{' '}
        <Link to="/login" className="text-accent hover:underline">
          Log in
        </Link>
      </p>
    </form>
  );
}
