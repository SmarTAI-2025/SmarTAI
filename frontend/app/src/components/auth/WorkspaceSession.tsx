import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useMemo, useSyncExternalStore, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useCurrentUser } from "@/api/hooks";
import { normalizeAPIError } from "@/api/client";
import { PageDraftSession } from "@/hooks/usePageDraft";
import { FrontierContext } from "@/lib/frontierContext";
import { getFrontierGeneration, getFrontierToken, isFrontierExitPending, isFrontierWorkspacePath, subscribeFrontierSession } from "@/lib/frontierSession";
import { RequireTeacherSession } from "./RequireTeacherSession";
import { SessionRestoreError } from "./SessionRestoreError";

export function WorkspaceSession({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const generation = useSyncExternalStore(subscribeFrontierSession, getFrontierGeneration);
  const guestQueries = useMemo(() => new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } } }), [generation]);
  if (!isFrontierWorkspacePath(pathname)) return <RequireTeacherSession>{children}</RequireTeacherSession>;
  return <FrontierContext.Provider value={true}><QueryClientProvider key={generation} client={guestQueries}>
    <RequireFrontierSession>{children}</RequireFrontierSession>
  </QueryClientProvider></FrontierContext.Provider>;
}

function RequireFrontierSession({ children }: { children: ReactNode }) {
  const token = useSyncExternalStore(subscribeFrontierSession, getFrontierToken);
  const exiting = useSyncExternalStore(subscribeFrontierSession, isFrontierExitPending);
  const user = useCurrentUser();
  if (!exiting && (!token || (user.isError && normalizeAPIError(user.error).status === 401))) return <Navigate to="/frontier/enter" replace />;
  if (user.isLoading) return <div role="status" className="p-8 text-center">Loading Demo…</div>;
  if (!exiting && user.isError) return <SessionRestoreError retry={() => void user.refetch()} busy={user.isFetching} />;
  if (!user.data) return null;
  return <PageDraftSession ownerId={user.data.id}>{children}</PageDraftSession>;
}
