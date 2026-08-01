import type { ReactNode } from "react";
import { useEffect } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { clearAuthToken } from "@/api/client";
import { useCurrentUser } from "@/api/hooks";
import { Card } from "@/components/ui/Card";
import type { UserRole } from "@/types/auth";

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
  const location = useLocation();
  const allowedRoles = Array.isArray(allowed) ? allowed : [allowed];

  if (currentUser.isLoading) {
    return (
      <main className="flex min-h-screen items-center justify-center bg-background px-4">
        <Card className="w-full max-w-sm text-center text-sm text-muted-foreground">
          正在恢复登录状态...
        </Card>
      </main>
    );
  }

  if (currentUser.isError || !currentUser.data) {
    return <ResetSessionAndRedirect message="登录状态已过期，请重新登录。" />;
  }

  const role = currentUser.data.role;
  if (!allowedRoles.includes(role)) {
    // Wrong role but valid session: redirect to that role's home, do NOT log out.
    return <Navigate to={homeFor(role)} replace />;
  }

  return <>{children}</>;
}

function ResetSessionAndRedirect({ message }: { message: string }) {
  useEffect(() => {
    clearAuthToken();
  }, []);
  return <Navigate to="/login" replace state={{ authError: message }} />;
}
