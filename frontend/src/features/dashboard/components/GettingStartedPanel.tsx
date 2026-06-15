import { Link } from 'react-router';

export interface GettingStartedPanelProps {
  slug: string;
}

interface Step {
  n: number;
  title: string;
  body: string;
  to: string;
  cta: string;
}

/**
 * Zero-streams onboarding (frontend-architecture §9.2, §10.3). The 4-step path to a
 * first live event with deep links — drives the PRD ≤ 15 min time-to-first-event.
 */
export function GettingStartedPanel({ slug }: GettingStartedPanelProps) {
  const steps: Step[] = [
    {
      n: 1,
      title: 'Pick a scenario',
      body: 'Choose a scenario and create an instance configured for this workspace.',
      to: `/w/${slug}/scenarios`,
      cta: 'Browse scenarios',
    },
    {
      n: 2,
      title: 'Create an API key',
      body: 'Generate a key with events:read so you can pull events over the API.',
      to: `/w/${slug}/api-keys`,
      cta: 'Create a key',
    },
    {
      n: 3,
      title: 'Start a stream',
      body: 'Pin your instance to a stream and start it to begin generating events.',
      to: `/w/${slug}/streams/new`,
      cta: 'Start a stream',
    },
    {
      n: 4,
      title: 'Watch live events',
      body: 'Open the live tail and watch events arrive in real time.',
      to: `/w/${slug}/monitoring`,
      cta: 'Open monitoring',
    },
  ];

  return (
    <section
      aria-labelledby="getting-started-heading"
      className="rounded-lg border border-border bg-surface p-6"
    >
      <h2 id="getting-started-heading" className="text-base font-semibold text-text">
        Get your first event flowing
      </h2>
      <p className="mt-1 text-sm text-text-muted">
        Four steps from an empty workspace to live events.
      </p>
      <ol className="mt-5 grid gap-4 sm:grid-cols-2">
        {steps.map((step) => (
          <li
            key={step.n}
            className="flex gap-3 rounded-md border border-border bg-surface-muted p-4"
          >
            <span
              aria-hidden
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-status-blue/15 text-sm font-semibold text-status-blue"
            >
              {step.n}
            </span>
            <div className="min-w-0">
              <h3 className="text-sm font-semibold text-text">{step.title}</h3>
              <p className="mt-0.5 text-sm text-text-muted">{step.body}</p>
              <Link
                to={step.to}
                className="mt-2 inline-block text-sm font-medium text-status-blue hover:underline focus-visible:underline focus-visible:outline-none"
              >
                {step.cta} →
              </Link>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
