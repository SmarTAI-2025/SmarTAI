import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { clearAuthToken, normalizeAPIError } from "@/api/client";
import { useCurrentUser } from "@/api/hooks";
import { Card } from "@/components/ui/Card";
import type { UserRole } from "@/types/auth";
import { useSessionExpired } from "@/lib/sessionExpiry";
import { SessionRestoreError } from "./SessionRestoreError";
import { useSessionActivity } from "@/hooks/useSessionActivity";

/**
 * Role-aware route guard. Wraps a workspace root so that a logged-in user of
 * the wrong role is redirected to their own role home (without clearing a valid
 * session), while an unauthenticated/expired session still clears the token and
 * bounces to /login. The previous "teacher-only" footer is replaced by the
 * current role + workspace label in the shell.
 */
export function RequireRoleSession({
  allowed,
  homeFor,
  children,
}: {
  allowed: UserRole | UserRole[];
  homeFor: (role: UserRole) => string;
  children: ReactNode;
}) {
  const currentUser = useCurrentUser();
  const expired = useSessionExpired();
  useSessionActivity(Boolean(currentUser.data) && !expired);
  const location = useLocation();
  const allowedRoles = Array.isArray(allowed) ? allowed : [allowed];

  if (!expired && currentUser.isLoading) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background px-4">
        <Card className="w-full max-w-sm text-center text-sm text-muted-foreground">
          正在恢复登录状态...
        </Card>
      </main>
    );
  }

  if (!expired && currentUser.isError && normalizeAPIError(currentUser.error).status !== 401) {
    return <SessionRestoreError retry={() => void currentUser.refetch()} busy={currentUser.isFetching} />;
  }

  if (expired || currentUser.isError || !currentUser.data) {
    return <ResetSessionAndRedirect message="登录状态已过期，请重新登录。" returnTo={`${location.pathname}${location.search}${location.hash}`} />;
  }

  const role = currentUser.data.role;
  if (!allowedRoles.includes(role)) {
    // Wrong role but valid session: redirect to that role's home, do NOT log out.
    return <Navigate to={homeFor(role)} replace />;
  }

  return <>{children}</>;
}

function ResetSessionAndRedirect({ message, returnTo }: { message: string; returnTo: string }) {
  const queryClient = useQueryClient();
  useEffect(() => {
    clearAuthToken();
    queryClient.clear();
  }, [queryClient]);
  return <Navigate to="/login" replace state={{ authError: message, from: returnTo }} />;
}
