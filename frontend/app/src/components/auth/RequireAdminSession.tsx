import { useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useEffect } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { clearAuthToken } from "@/api/client";
import { useCurrentUser } from "@/api/hooks";
import { AuthCard, AuthFrame } from "@/components/auth/AuthFrame";

export function RequireAdminSession({ children }: { children: ReactNode }) {
  const currentUser = useCurrentUser();
  const location = useLocation();
  const queryClient = useQueryClient();
  const returnTo = `${location.pathname}${location.search}${location.hash}`;

  if (currentUser.isLoading) {
    return <AuthFrame><AuthCard><div className="py-8 text-center text-sm text-muted-foreground">正在恢复管理员登录状态…</div></AuthCard></AuthFrame>;
  }
  if (currentUser.isError || !currentUser.data || currentUser.data.role !== "admin") {
    return <ResetAdminSession returnTo={returnTo} queryClient={queryClient} />;
  }
  return children;
}

function ResetAdminSession({ returnTo, queryClient }: { returnTo: string; queryClient: ReturnType<typeof useQueryClient> }) {
  useEffect(() => {
    clearAuthToken();
    queryClient.clear();
  }, [queryClient]);
  return <Navigate to="/login" replace state={{ authError: "需要管理员账号登录。", from: returnTo }} />;
}
