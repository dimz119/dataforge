import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { useNavigate } from 'react-router';

import { useLogout } from '../../features/auth';
import { cn } from '../../shared/lib/cn';
import { useToast } from '../../shared/ui';

/**
 * Top-bar user menu (frontend-architecture §3.1). Logout runs the full §6.4
 * sequence (server blacklist → TokenManager clear → queryClient.clear via the
 * mutation's invalidation → broadcast) then routes to /login.
 */
export function UserMenu({ email }: { email: string }) {
  const navigate = useNavigate();
  const logout = useLogout();
  const toast = useToast();

  const onLogout = () => {
    logout.mutate(undefined, {
      onSuccess: () => {
        void navigate('/login', { replace: true });
      },
      onError: (err) => toast.showError(err, 'Logout failed'),
    });
  };

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        className={cn(
          'inline-flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-text',
          'hover:bg-surface-muted focus:outline-none',
        )}
      >
        <span className="grid h-7 w-7 place-items-center rounded-full bg-accent/10 text-xs font-semibold text-accent">
          {email.slice(0, 1).toUpperCase()}
        </span>
        <span className="max-w-[14rem] truncate text-text-muted">{email}</span>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          className="z-40 min-w-44 rounded-md border border-border bg-surface p-1 shadow-md"
        >
          <DropdownMenu.Item
            onSelect={onLogout}
            className="cursor-pointer rounded px-2 py-1.5 text-sm text-text outline-none data-[highlighted]:bg-surface-muted"
          >
            Log out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
