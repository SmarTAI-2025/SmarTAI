import { createContext, useContext, type ReactNode } from "react";
import { useHealthStatus } from "@/api/hooks/health";
import type { WorkflowTimeoutMinutes } from "@/types/health";

const WorkflowTimeouts = createContext<WorkflowTimeoutMinutes | undefined>(undefined);

export function WorkflowTimeoutsProvider({ children }: { children: ReactNode }) {
  const { data } = useHealthStatus({ refetchOnWindowFocus: true });
  return <WorkflowTimeouts.Provider value={data?.workflow_timeout_minutes}>{children}</WorkflowTimeouts.Provider>;
}

export function useWorkflowTimeouts() {
  return useContext(WorkflowTimeouts);
}
