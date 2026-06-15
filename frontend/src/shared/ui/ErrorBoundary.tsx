import { Component, type ErrorInfo, type ReactNode } from 'react';

import { ErrorState } from './ErrorState';

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Custom fallback; defaults to <ErrorState> with a reload action. */
  fallback?: (error: unknown, reset: () => void) => ReactNode;
}

interface ErrorBoundaryState {
  error: unknown;
}

/**
 * Top-level render error boundary (frontend-architecture §10). Catches render
 * exceptions (not async query errors — those flow through ApiError + ErrorState).
 * Mounted once at the composition root and reusable per-route by feature code.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    // Observability hook-up (logging/Sentry) lands later; keep a console trace now.
    console.error('Render error boundary caught:', error, info.componentStack);
  }

  reset = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;
    if (error != null) {
      if (this.props.fallback) return this.props.fallback(error, this.reset);
      return (
        <div className="p-8">
          <ErrorState error={error} onRetry={this.reset} />
        </div>
      );
    }
    return this.props.children;
  }
}
