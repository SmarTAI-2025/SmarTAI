import { ClipboardList } from "lucide-react";
import type { FormEvent } from "react";
import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { clearAuthToken, normalizeAPIError } from "@/api/client";
import { useLogin } from "@/api/hooks";
import { authKeys } from "@/api/hooks/keys";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const login = useLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [formError, setFormError] = useState<string | null>(
    location.state && typeof location.state === "object" && "authError" in location.state
      ? String(location.state.authError)
      : null,
  );

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);

    try {
      const response = await login.mutateAsync({
        username: username.trim(),
        password,
      });

      if (response.user.role !== "teacher" && response.user.role !== "admin") {
        clearAuthToken();
        queryClient.removeQueries({ queryKey: authKeys.me });
        setFormError("当前 React 前端仅开放教师端流程，请使用教师账号登录。");
        return;
      }

      navigate("/", { replace: true });
    } catch (error) {
      setFormError(normalizeAPIError(error).message);
    }
  }

  return (
    <AuthFrame>
      <Card className="w-full max-w-md p-6">
        <div className="text-center">
          <ClipboardList className="mx-auto h-10 w-10 text-primary" />
          <h1 className="mt-3 text-2xl font-semibold">登录 SmarTAI</h1>
          <p className="mt-1 text-sm text-muted-foreground">进入教师端批改工作台。</p>
        </div>
        <form className="mt-6 grid gap-4" onSubmit={handleSubmit}>
          <Field label="用户名">
            <Input
              autoComplete="username"
              disabled={login.isPending}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="username"
              required
              value={username}
            />
          </Field>
          <Field label="密码">
            <Input
              autoComplete="current-password"
              disabled={login.isPending}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="password"
              required
              type="password"
              value={password}
            />
          </Field>
          {formError ? (
            <div className="rounded-md border border-danger/40 bg-danger/10 p-3 text-sm text-danger">
              {formError}
            </div>
          ) : null}
          <Button type="submit" className="w-full" disabled={login.isPending}>
            {login.isPending ? "登录中..." : "登录"}
          </Button>
        </form>
        <div className="mt-5 text-center text-sm text-muted-foreground">
          还没有账号？{" "}
          <Link className="font-medium text-primary" to="/register">
            申请注册
          </Link>
        </div>
      </Card>
    </AuthFrame>
  );
}

function AuthFrame({ children }: { children: React.ReactNode }) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4">
      {children}
    </main>
  );
}
