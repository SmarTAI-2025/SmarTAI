import { Loader2, TicketCheck } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { clearAuthToken } from "@/api/client";
import { useRegister } from "@/api/hooks";
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

export function RegisterPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const register = useRegister();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const nextUsername = username.trim();
    const nextEmail = email.trim();
    const nextInvite = inviteCode.trim().toUpperCase();

    if (nextUsername.length < 3) {
      setFormError(
        zh ? "用户名至少需要 3 个字符。" : "Username must contain at least 3 characters.",
      );
      return;
    }
    if (!nextEmail || !nextInvite) {
      setFormError(
        zh ? "请填写受邀邮箱和邀请码。" : "Enter the invited email and invitation code.",
      );
      return;
    }
    if (password.length < 6) {
      setFormError(
        zh ? "密码至少需要 6 个字符。" : "Password must contain at least 6 characters.",
      );
      return;
    }
    if (password !== confirmation) {
      setFormError(zh ? "两次输入的密码不一致。" : "The passwords do not match.");
      return;
    }

    try {
      const response = await register.mutateAsync({
        username: nextUsername,
        email: nextEmail,
        password,
        invite_code: nextInvite,
      });
      if (response.user.role !== "teacher" && response.user.role !== "admin") {
        clearAuthToken();
        queryClient.clear();
        navigate("/student", { replace: true });
        return;
      }
      navigate("/", { replace: true });
    } catch (error) {
      setPassword("");
      setConfirmation("");
      setFormError(localizedAuthError(error, locale, "register"));
    }
  }

  return (
    <AuthFrame>
      <AuthCard>
        <div className="flex items-center gap-3">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] bg-blue-50 text-primary dark:bg-blue-950/50">
            <TicketCheck aria-hidden="true" size={21} />
          </span>
          <div>
            <p className="text-xs font-semibold text-primary">
              {zh ? "邀请制测试" : "Invite-only testing"}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {zh ? "邀请码一次有效" : "Invitation codes are single-use"}
            </p>
          </div>
        </div>

        <h1 className="mt-5 text-[27px] font-semibold tracking-[-0.025em]">
          {zh ? "创建受邀账号" : "Create invited account"}
        </h1>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {zh
            ? "请使用邀请邮件中的邮箱和邀请码。公开注册当前未开放。"
            : "Use the email and code from your invitation. Public registration is currently closed."}
        </p>

        <form className="mt-6 grid gap-3.5" onSubmit={handleSubmit}>
          <div className="grid gap-3.5 sm:grid-cols-2">
            <Field label={zh ? "用户名" : "Username"}>
              <Input
                className="h-11 w-full"
                autoComplete="username"
                autoFocus
                disabled={register.isPending}
                minLength={3}
                maxLength={64}
                onChange={(event) => setUsername(event.target.value)}
                placeholder={zh ? "至少 3 个字符" : "At least 3 characters"}
                required
                value={username}
              />
            </Field>
            <Field label={zh ? "受邀邮箱" : "Invited email"}>
              <Input
                className="h-11 w-full"
                autoComplete="email"
                disabled={register.isPending}
                maxLength={254}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="name@example.com"
                required
                type="email"
                value={email}
              />
            </Field>
          </div>
          <Field
            label={zh ? "邀请码" : "Invitation code"}
            hint={zh ? "不区分大小写，使用成功后立即失效。" : "Case-insensitive and consumed after successful use."}
          >
            <Input
              className="h-11 w-full uppercase tracking-[0.16em]"
              autoComplete="one-time-code"
              disabled={register.isPending}
              onChange={(event) => setInviteCode(event.target.value)}
              placeholder={zh ? "输入邀请码" : "Enter invitation code"}
              required
              value={inviteCode}
            />
          </Field>
          <div className="grid gap-3.5 sm:grid-cols-2">
            <Field label={zh ? "设置密码" : "Password"}>
              <AuthPasswordInput
                autoComplete="new-password"
                disabled={register.isPending}
                onChange={(event) => setPassword(event.target.value)}
                placeholder={zh ? "至少 6 个字符" : "At least 6 characters"}
                value={password}
                showLabel={zh ? "显示密码" : "Show password"}
                hideLabel={zh ? "隐藏密码" : "Hide password"}
              />
            </Field>
            <Field label={zh ? "确认密码" : "Confirm password"}>
              <AuthPasswordInput
                autoComplete="new-password"
                disabled={register.isPending}
                onChange={(event) => setConfirmation(event.target.value)}
                placeholder={zh ? "再次输入密码" : "Enter password again"}
                value={confirmation}
                showLabel={zh ? "显示确认密码" : "Show confirmation password"}
                hideLabel={zh ? "隐藏确认密码" : "Hide confirmation password"}
              />
            </Field>
          </div>
          {formError ? <AuthError message={formError} /> : null}
          <Button type="submit" className="mt-1 h-11 w-full" disabled={register.isPending}>
            {register.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}
            {register.isPending
              ? zh
                ? "正在创建…"
                : "Creating account…"
              : zh
                ? "创建账号"
                : "Create account"}
          </Button>
        </form>

        <div className="mt-5 border-t pt-5 text-center text-sm text-muted-foreground">
          {zh ? "已有账号？" : "Already have an account?"}{" "}
          <Link
            className="font-semibold text-primary outline-none hover:underline focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
            to="/login"
          >
            {zh ? "返回登录" : "Back to sign in"}
          </Link>
        </div>
      </AuthCard>
    </AuthFrame>
  );
}
