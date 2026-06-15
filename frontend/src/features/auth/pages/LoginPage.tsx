import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { Link, useLocation, useNavigate } from 'react-router';

import { mapValidationProblem } from '../../../shared/api/formErrors';
import { ApiError } from '../../../shared/api/problem';
import { Button, FormField, Input } from '../../../shared/ui';
import { useLogin } from '../api';
import { loginSchema, type LoginValues } from '../schemas';

interface ReturnToState {
  returnTo?: string;
}

/**
 * Login page (frontend-architecture §9.1 LoginForm). Email + password; pending
 * state; problem-details errors mapped to fields/banner; on success restores the
 * `returnTo` set by RequireAuth (§3.2).
 */
export function LoginPage() {
  const login = useLogin();
  const navigate = useNavigate();
  const location = useLocation();
  const returnTo = (location.state as ReturnToState | null)?.returnTo ?? '/';

  const {
    register,
    handleSubmit,
    setError,
    formState: { errors },
  } = useForm<LoginValues>({ resolver: zodResolver(loginSchema) });

  const onSubmit = handleSubmit((values) => {
    login.mutate(values, {
      onSuccess: () => {
        void navigate(returnTo, { replace: true });
      },
      onError: (err) => {
        const mapped = mapValidationProblem(err, ['email', 'password']);
        if (mapped.fields.email) setError('email', { message: mapped.fields.email });
        if (mapped.fields.password) setError('password', { message: mapped.fields.password });
        if (mapped.formLevel.length > 0) setError('root', { message: mapped.formLevel.join(' ') });
        else if (err instanceof ApiError && mapped.formLevel.length === 0 && !mapped.fields.email)
          setError('root', { message: err.detail ?? err.title });
      },
    });
  });

  return (
    <form
      onSubmit={(e) => {
        void onSubmit(e);
      }}
      noValidate
      className="flex flex-col gap-4"
    >
      <h1 className="text-lg font-semibold text-text">Log in</h1>
      {errors.root && (
        <p role="alert" className="rounded-md bg-danger/10 px-3 py-2 text-sm text-danger">
          {errors.root.message}
        </p>
      )}
      <FormField label="Email" error={errors.email?.message}>
        {(p) => <Input type="email" autoComplete="email" {...p} {...register('email')} />}
      </FormField>
      <FormField label="Password" error={errors.password?.message}>
        {(p) => (
          <Input type="password" autoComplete="current-password" {...p} {...register('password')} />
        )}
      </FormField>
      <Button type="submit" loading={login.isPending}>
        Log in
      </Button>
      <div className="flex justify-between text-sm text-text-muted">
        <Link to="/forgot-password" className="text-accent hover:underline">
          Forgot password?
        </Link>
        <Link to="/signup" className="text-accent hover:underline">
          Create account
        </Link>
      </div>
    </form>
  );
}
