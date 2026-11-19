import { useRef } from "react";
import { APIError } from "@/api/client";

/** Keep an uncertain HTTP attempt stable; a later explicit retry gets a new id. */
export function useGradingIntent() {
  type Intent = { requestId: string; expectedWorkflowRevision: number };
  const pending = useRef<{ intent: Intent; selectionKey?: string } | null>(null);
  const inFlight = useRef(false);
  return {
    async execute<T>(revision: number, submit: (intent: Intent) => Promise<T>, selectionKey?: string) {
      if (inFlight.current) return undefined;
      inFlight.current = true;
      if (!pending.current || pending.current.selectionKey !== selectionKey) {
        // A changed selection is a new intent. The server still fences the
        // revision and deduplicates active grading runs for this task.
        pending.current = { intent: { requestId: crypto.randomUUID(), expectedWorkflowRevision: revision }, selectionKey };
      }
      try {
        const response = await submit(pending.current.intent);
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
