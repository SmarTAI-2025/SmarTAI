import { useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { clearAuthToken } from "@/api/client";
import { useCurrentUser } from "@/api/hooks";
import { AuthCard, AuthFrame } from "@/components/auth/AuthFrame";
import { useI18n } from "@/i18n/I18nProvider";

export function RequireTeacherSession({ children }: { children: ReactNode }) {
  const currentUser = useCurrentUser();
  const location = useLocation();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const returnTo = `${location.pathname}${location.search}${location.hash}`;

  if (currentUser.isLoading) {
    return (
      <AuthFrame>
        <AuthCard>
          <div
            role="status"
            className="flex items-center justify-center gap-2 py-8 text-sm font-medium text-muted-foreground"
          >
            <Loader2 aria-hidden="true" className="animate-spin text-primary" size={17} />
            {zh ? "正在恢复登录状态…" : "Restoring your session…"}
          </div>
        </AuthCard>
      </AuthFrame>
    );
  }

  if (currentUser.isError || !currentUser.data) {
    return (
      <ResetSessionAndRedirect
        message={zh ? "登录状态已过期，请重新登录。" : "Your session expired. Sign in again."}
        returnTo={returnTo}
      />
    );
  }

  if (currentUser.data.role !== "teacher" && currentUser.data.role !== "admin") {
    return (
      <ResetSessionAndRedirect
        message={
          zh
            ? "当前 React 前端仅开放教师端流程，请使用教师账号登录。"
            : "This workspace is currently available to teachers only. Sign in with a teacher account."
        }
        returnTo={returnTo}
      />
    );
  }

  return children;
}

function ResetSessionAndRedirect({ message, returnTo }: { message: string; returnTo: string }) {
  const queryClient = useQueryClient();

  useEffect(() => {
    clearAuthToken();
    queryClient.clear();
  }, [queryClient]);

  return <Navigate to="/login" replace state={{ authError: message, from: returnTo }} />;
}
