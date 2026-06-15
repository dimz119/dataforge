import { controlRow } from '../controlMatrix';

export interface StreamDangerZoneProps {
  status: string;
}

/**
 * Stream danger zone (frontend-architecture §9.5 StreamDangerZone): stop + delete,
 * delete only in created/stopped/failed (T14). Stop lives with the LifecycleButtons
 * (one Stop control, matrix-driven). Delete (`DELETE /streams/{id}`) is NOT in the
 * MVP OpenAPI contract — the endpoint is absent from the generated client and the
 * backend StreamDetailView exposes GET | PATCH only — so the delete control renders
 * disabled here with the reason. The matrix already reserves the slot per status;
 * wiring becomes a one-line `useDeleteStream` when the contract adds the verb.
 */
export function StreamDangerZone({ status }: StreamDangerZoneProps) {
  const row = controlRow(status);
  const deleteAllowed = row.delete === 'enabled';

  return (
    <section className="rounded-lg border border-danger/40 bg-danger/5 p-5">
      <h2 className="text-sm font-semibold text-text">Danger zone</h2>
      <p className="mt-1 text-sm text-text-muted">
        Stopping is available from the controls above. Deleting a stream is permitted only
        when it is created, stopped, or failed (T14).
      </p>
      <button
        type="button"
        disabled
        title="Stream deletion is not yet available in this API version."
        className="mt-4 inline-flex h-10 cursor-not-allowed items-center rounded-md border border-border px-4 text-sm font-medium text-text-muted opacity-60"
      >
        Delete stream
      </button>
      <p className="mt-2 text-xs text-text-muted">
        {deleteAllowed
          ? 'Deletion endpoint not available in this API version yet.'
          : 'This stream cannot be deleted in its current state.'}
      </p>
    </section>
  );
}
