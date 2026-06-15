import { z } from 'zod';

/**
 * Client-side form schemas mirroring the API constraints (frontend-architecture
 * §10.4). The API remains authoritative; these give instant feedback. Password
 * policy: ≥ 10 chars (api-specification §4.1; full policy owned by the security
 * architecture).
 */
const password = z.string().min(10, 'Use at least 10 characters.').max(128, 'Too long.');

export const loginSchema = z.object({
  email: z.string().email('Enter a valid email.'),
  password: z.string().min(1, 'Enter your password.'),
});
export type LoginValues = z.infer<typeof loginSchema>;

export const signupSchema = z
  .object({
    email: z.string().email('Enter a valid email.'),
    password,
    confirm: z.string(),
  })
  .refine((v) => v.password === v.confirm, {
    message: 'Passwords do not match.',
    path: ['confirm'],
  });
export type SignupValues = z.infer<typeof signupSchema>;

export const emailSchema = z.object({ email: z.string().email('Enter a valid email.') });
export type EmailValues = z.infer<typeof emailSchema>;

export const resetSchema = z
  .object({ password, confirm: z.string() })
  .refine((v) => v.password === v.confirm, {
    message: 'Passwords do not match.',
    path: ['confirm'],
  });
export type ResetValues = z.infer<typeof resetSchema>;
