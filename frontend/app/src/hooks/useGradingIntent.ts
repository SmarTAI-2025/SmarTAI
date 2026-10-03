import { useRef } from "react";
import { APIError } from "@/api/client";

/** Keep an uncertain HTTP attempt stable; a later explicit retry gets a new id. */
export function useGradingIntent() {
  const pending = useRef<{ requestId: string; expectedWorkflowRevision: number } | null>(null);
  const inFlight = useRef(false);
  return {
    async execute<T>(revision: number, submit: (intent: { requestId: string; expectedWorkflowRevision: number }) => Promise<T>) {
      if (inFlight.current) return undefined;
      inFlight.current = true;
      pending.current ??= { requestId: crypto.randomUUID(), expectedWorkflowRevision: revision };
      try {
        const response = await submit(pending.current);
        pending.current = null;
        return response;
      } catch (error) {
        // A definite rejection (permissions/config/revision) can be repaired.
        // A timeout, disconnect or server error may have committed the run.
        if (error instanceof APIError && error.status >= 400 && error.status < 500) pending.current = null;
        throw error;
      } finally {
        inFlight.current = false;
      }
    },
  };
}
