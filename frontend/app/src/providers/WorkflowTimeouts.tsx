import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useHealthStatus } from "@/api/hooks/health";
import type { WorkflowTimeoutMinutes } from "@/types/health";

const WorkflowTimeouts = createContext<{
  limits?: WorkflowTimeoutMinutes;
  subscribe: () => () => void;
} | undefined>(undefined);

export function WorkflowTimeoutsProvider({ children }: { children: ReactNode }) {
  const [subscribers, setSubscribers] = useState(0);
  const subscribe = useCallback(() => {
    setSubscribers((count) => count + 1);
    return () => setSubscribers((count) => count - 1);
  }, []);
  const { data } = useHealthStatus({ enabled: subscribers > 0, refetchOnWindowFocus: true });
  const limits = data?.workflow_timeout_minutes;
  const value = useMemo(() => ({ limits, subscribe }), [limits, subscribe]);
  return <WorkflowTimeouts.Provider value={value}>{children}</WorkflowTimeouts.Provider>;
}

export function useWorkflowTimeouts(needed: boolean) {
  const context = useContext(WorkflowTimeouts);
  const subscribe = context?.subscribe;
  useEffect(() => needed ? subscribe?.() : undefined, [needed, subscribe]);
  return context?.limits;
}
