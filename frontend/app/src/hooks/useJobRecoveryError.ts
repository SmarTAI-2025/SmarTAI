import { useLayoutEffect, useMemo, useRef, useState } from "react";

/** An action from a previous job must not lock the current job's recovery controls. */
export function useJobRecoveryError(taskId?: string, jobId?: string | null) {
  const scope = useMemo(() => ({ taskId, jobId }), [taskId, jobId]);
  const active = useRef<typeof scope | null>(null);
  const [failure, setFailure] = useState<{ scope: typeof scope; error: unknown } | null>(null);
  useLayoutEffect(() => {
    active.current = scope;
    return () => { active.current = null; };
  }, [scope]);
  const setError = (error: unknown) => {
    if (active.current === scope) setFailure(error == null ? null : { scope, error });
  };
  return [failure?.scope === scope ? failure.error : null, setError] as const;
}
