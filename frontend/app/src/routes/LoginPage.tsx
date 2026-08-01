import { FileCheck2, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { clearAuthToken } from "@/api/client";
import { useLogin } from "@/api/hooks";
import { authKeys } from "@/api/hooks/keys";
import {
  AuthCard,
  AuthError,
  AuthFrame,
  AuthPasswordInput,
} from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";
import { localizedAuthError } from "@/lib/authErrors";

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const login = useLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [stateErrorDismissed, setStateErrorDismissed] = useState(false);
  const stateError = getAuthStateError(location.state);
  const visibleError =
    formError ??
    (!stateErrorDismissed && stateError ? localizedSessionError(stateError, locale) : null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    setStateErrorDismissed(true);
    const normalizedUsername = username.trim();
    if (!normalizedUsername || !password) {
      setFormError(
        zh ? "请输入用户名和密码。" : "Enter your username and password.",
      );
      return;
    }

    try {
      const response = await login.mutateAsync({
        username: normalizedUsername,
        password,
      });

      if (response.user.role !== "teacher" && response.user.role !== "admin") {
        clearAuthToken();
        queryClient.clear();
        setPassword("");
        setFormError(
          zh
            ? "当前工作台仅开放教师端，请使用教师账号登录。"
            : "This workspace is currently for teachers. Sign in with a teacher account.",
        );
        return;
      }

      queryClient.removeQueries({
        predicate: (query) => query.queryKey[0] !== authKeys.me[0],
      });
      navigate(safeReturnPath(location.state), { replace: true });
    } catch (error) {
      setPassword("");
      setFormError(localizedAuthError(error, locale, "login"));
    }
  }

  return (
    <AuthFrame>
      <AuthCard>
        <div className="flex items-center gap-3">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] bg-blue-50 text-primary dark:bg-blue-950/50">
            <FileCheck2 aria-hidden="true" size={21} />
          </span>
          <div>
            <p className="text-xs font-semibold text-primary">
              {zh ? "教师端" : "Teacher workspace"}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {zh ? "题目 · 作答 · 复核 · 分析" : "Questions · Submissions · Review · Insights"}
            </p>
          </div>
        </div>

        <h1 className="mt-5 text-[27px] font-semibold tracking-[-0.025em]">
          {zh ? "SmarTAI 智能批改工作台" : "SmarTAI Intelligent Grading Workspace"}
        </h1>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {zh
            ? "登录后继续处理批改任务、教师复核与结果分析。"
            : "Sign in to continue grading tasks, teacher review, and results analysis."}
        </p>

        <form className="mt-7 grid gap-4" onSubmit={handleSubmit}>
          <Field label={zh ? "用户名" : "Username"}>
            <Input
              className="h-11 w-full"
              autoComplete="username"
              autoFocus
              disabled={login.isPending}
              onChange={(event) => setUsername(event.target.value)}
              placeholder={zh ? "输入用户名" : "Enter username"}
              required
              value={username}
            />
          </Field>
          <Field label={zh ? "密码" : "Password"}>
            <AuthPasswordInput
              autoComplete="current-password"
              disabled={login.isPending}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={zh ? "输入密码" : "Enter password"}
              value={password}
              showLabel={zh ? "显示密码" : "Show password"}
              hideLabel={zh ? "隐藏密码" : "Hide password"}
            />
          </Field>
          {visibleError ? <AuthError message={visibleError} /> : null}
          <Button type="submit" className="mt-1 h-11 w-full" disabled={login.isPending}>
            {login.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}
            {login.isPending ? (zh ? "正在登录…" : "Signing in…") : zh ? "登录" : "Sign in"}
          </Button>
        </form>

        <div className="mt-6 border-t pt-5 text-center text-sm text-muted-foreground">
          {zh ? "收到测试邀请？" : "Received a testing invitation?"}{" "}
          <Link
            className="font-semibold text-primary outline-none hover:underline focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
            to="/register"
          >
            {zh ? "使用邀请码注册" : "Create invited account"}
          </Link>
        </div>
      </AuthCard>
    </AuthFrame>
  );
}

function getAuthStateError(state: unknown): string | null {
  return state && typeof state === "object" && "authError" in state
    ? String(state.authError)
    : null;
}

function localizedSessionError(value: string, locale: "zh-CN" | "en-US") {
  if (locale === "zh-CN") return value;
  if (value.includes("过期")) return "Your session expired. Sign in again.";
  if (value.includes("教师")) return "This workspace is currently available to teachers only.";
  return "Sign in again to continue.";
}

function safeReturnPath(state: unknown): string {
  if (!state || typeof state !== "object" || !("from" in state)) return "/";
  const from = String(state.from);
  try {
    const parsed = new URL(from, window.location.origin);
    if (
      parsed.origin !== window.location.origin ||
      parsed.pathname === "/login" ||
      parsed.pathname === "/register"
    ) {
      return "/";
    }
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return "/";
  }
}
