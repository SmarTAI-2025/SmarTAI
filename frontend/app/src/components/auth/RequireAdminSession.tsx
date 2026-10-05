import { PageDraftSession } from "@/hooks/useDraftProtection";
import { useI18n } from "@/i18n/I18nProvider";
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, type ReactNode } from "react";
import { Navigate, useLocation, Link } from "react-router-dom";
import { clearAuthToken, normalizeAPIError } from "@/api/client";
import { useCurrentUser, useLogout } from "@/api/hooks";
import { AuthCard, AuthFrame } from "./AuthFrame";
import { SessionRestoreError } from "./SessionRestoreError";
import { useSessionExpired } from "@/lib/sessionExpiry";
import { Button } from "@/components/ui/Button";
import { useSessionActivity } from "@/hooks/useSessionActivity";

export function RequireAdminSession({ children }: { children: ReactNode }) {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const user = useCurrentUser();
  const expired = useSessionExpired();
  useSessionActivity(Boolean(user.data) && !expired);
  const location = useLocation();
  const logout = useLogout();
  const from = `${location.pathname}${location.search}${location.hash}`;
  if (!expired && user.isLoading) return <AuthFrame><AuthCard><p role="status">{zh ? "正在恢复管理员登录状态…" : "Restoring administrator session…"}</p></AuthCard></AuthFrame>;
  if (!expired && user.isError && normalizeAPIError(user.error).status !== 401) {
    return <SessionRestoreError retry={() => void user.refetch()} busy={user.isFetching} />;
  }
  if (expired || user.isError || !user.data) return <SignInAgain from={from} />;
  if (user.data.role !== "admin" || user.data.is_read_only) return <AuthFrame><AuthCard>
    <h1 className="text-xl font-semibold">{zh ? "没有管理权限" : "Administrator access required"}</h1>
    <p className="my-4 text-sm text-muted-foreground">{zh ? "当前账号不能进入管理端。历史批改任务不会因此删除。" : "This account cannot access administration. Existing grading tasks are preserved."}</p>
    <Button disabled={logout.isPending} onClick={() => logout.mutate()}>{zh ? "退出当前账号" : "Sign out"}</Button>
    <Link className="ml-4 text-primary" to="/login">{zh ? "更换账号" : "Switch account"}</Link>
  </AuthCard></AuthFrame>;
  return <PageDraftSession ownerId={user.data.id}>{children}</PageDraftSession>;
}
function SignInAgain({ from }: { from: string }) {
  const cache = useQueryClient();
  useEffect(() => { clearAuthToken(); cache.clear(); }, [cache]);
  return <Navigate to="/login" replace state={{ from, authError: "请登录管理员账号后继续。" }} />;
}
