import { CheckCircle2, Clock3, KeyRound, Link2Off, Loader2, type LucideIcon } from "lucide-react";
import { useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { clearAuthToken } from "@/api/client";
import { useConfirmPasswordReset } from "@/api/hooks";
import { AuthCard, AuthError, AuthFrame, AuthPasswordInput } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { useI18n } from "@/i18n/I18nProvider";
import { formatCooldown, rateLimitDelay, useRetryCooldown } from "@/hooks/useRetryCooldown";
import {
  localizedPasswordResetError,
  passwordResetLinkFailure,
  type LinkFailure,
} from "@/lib/authErrors";
import { clearPendingRegistrationFlow } from "@/lib/registrationFlow";
import { clearPasswordResetRequestMarker } from "@/lib/passwordResetRequestFlow";

type ResetState = "ready" | "success" | LinkFailure;

export function ResetPasswordPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const confirmReset = useConfirmPasswordReset();
  const cooldown = useRetryCooldown();
  const tokenRef = useRef<string | null | undefined>(undefined);
  if (tokenRef.current === undefined) tokenRef.current = resetTokenFromHash(location.hash);
  const submitting = useRef(false);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [state, setState] = useState<ResetState>(tokenRef.current ? "ready" : "invalid");

  useLayoutEffect(() => {
    if (!location.hash) return;
    const cleanLocation = `${location.pathname}${location.search}`;
    if (window.location.hash) {
      window.history.replaceState(window.history.state, "", cleanLocation);
    }
    let active = true;
    queueMicrotask(() => {
      if (active) navigate(cleanLocation, { replace: true });
    });
    return () => {
      active = false;
    };
  }, []); // Extract once, then remove the secret fragment before interaction.

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!tokenRef.current || submitting.current || confirmReset.isPending || cooldown.active) return;
    setFormError(null);
    if (password.length < 8 || password.length > 128) {
      setFormError(zh ? "新密码需为 8–128 个字符。" : "The new password must contain 8–128 characters.");
      return;
    }
    if (password !== confirmation) {
      setFormError(zh ? "两次输入的密码不一致。" : "The passwords do not match.");
      return;
    }

    submitting.current = true;
    const token = tokenRef.current;
    const nextPassword = password;
    tokenRef.current = null;
    setPassword("");
    setConfirmation("");
    try {
      await confirmReset.mutateAsync({ token, newPassword: nextPassword });
      confirmReset.reset();
      clearAuthToken();
      clearPendingRegistrationFlow();
      clearPasswordResetRequestMarker();
      queryClient.clear();
      setState("success");
    } catch (error) {
      confirmReset.reset();
      const failure = passwordResetLinkFailure(error);
      if (failure === "retry") tokenRef.current = token;
      cooldown.start(rateLimitDelay(error, "password_reset_rate_limited"));
      setFormError(localizedPasswordResetError(error, locale));
      setState(failure);
    } finally {
      submitting.current = false;
    }
  }

  if (state === "success") {
    return (
      <ResetResult
        icon={CheckCircle2}
        title={zh ? "密码已重置" : "Password reset"}
        description={zh
          ? "旧登录已由服务端撤销，本地访问令牌与缓存也已清空。请使用新密码重新登录。"
          : "The server revoked old sessions, and local access state was cleared. Sign in again with the new password."}
        actionLabel={zh ? "返回登录" : "Back to sign in"}
        onAction={() => navigate("/login", { replace: true })}
      />
    );
  }

  if (state === "expired" || state === "used" || state === "invalid") {
    const copy = state === "expired"
      ? {
          icon: Clock3,
          title: zh ? "重置链接已过期" : "Reset link expired",
          description: zh ? "请重新申请密码重置邮件。" : "Request a new password reset email.",
        }
      : state === "used"
        ? {
            icon: CheckCircle2,
            title: zh ? "重置链接已使用" : "Reset link already used",
            description: zh ? "请直接尝试登录，或重新申请密码重置邮件。" : "Try signing in, or request a new password reset email.",
          }
        : {
            icon: Link2Off,
            title: zh ? "重置链接无效" : "Reset link invalid",
            description: zh ? "请确认链接完整，或重新申请密码重置邮件。" : "Check the full link, or request a new password reset email.",
          };
    return (
      <ResetResult
        icon={copy.icon}
        title={copy.title}
        description={copy.description}
        actionLabel={zh ? "重新申请" : "Request a new link"}
        onAction={() => navigate("/forgot-password", { replace: true })}
        secondaryLabel={zh ? "返回登录" : "Back to sign in"}
        onSecondary={() => navigate("/login", { replace: true })}
      />
    );
  }

  return (
    <AuthFrame>
      <AuthCard>
        <div className="flex items-center gap-3">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] bg-blue-50 text-primary dark:bg-blue-950/50">
            <KeyRound aria-hidden="true" size={21} />
          </span>
          <div>
            <p className="text-xs font-semibold text-primary">{zh ? "账号安全" : "Account security"}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">{zh ? "打开页面不会消费链接" : "Opening this page does not consume the link"}</p>
          </div>
        </div>
        <h1 className="mt-5 text-[27px] font-semibold">{zh ? "设置新密码" : "Set a new password"}</h1>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {zh ? "提交后旧密码和旧登录状态将失效；不会自动登录。" : "After submission, the old password and sessions are invalidated. This flow does not sign you in."}
        </p>
        <form className="mt-6 grid gap-3.5" onSubmit={handleSubmit}>
          <Field label={zh ? "新密码" : "New password"}>
            <AuthPasswordInput
              autoComplete="new-password"
              disabled={confirmReset.isPending}
              minLength={8}
              maxLength={128}
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              showLabel={zh ? "显示密码" : "Show password"}
              hideLabel={zh ? "隐藏密码" : "Hide password"}
            />
          </Field>
          <Field label={zh ? "确认新密码" : "Confirm new password"}>
            <AuthPasswordInput
              autoComplete="new-password"
              disabled={confirmReset.isPending}
              minLength={8}
              maxLength={128}
              required
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              showLabel={zh ? "显示确认密码" : "Show confirmation password"}
              hideLabel={zh ? "隐藏确认密码" : "Hide confirmation password"}
            />
          </Field>
          {formError ? <AuthError message={formError} /> : null}
          <Button type="submit" className="mt-1 h-11 w-full" disabled={confirmReset.isPending || cooldown.active}>
            {confirmReset.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}
            {confirmReset.isPending
              ? (zh ? "正在重置…" : "Resetting…")
              : cooldown.active
                ? (zh ? `${formatCooldown(cooldown.seconds)} 后可重试` : `Retry in ${formatCooldown(cooldown.seconds)}`)
                : state === "retry"
                ? (zh ? "重新输入并重试" : "Re-enter and retry")
                : (zh ? "重置密码" : "Reset password")}
          </Button>
        </form>
        <div className="mt-5 border-t pt-5 text-center text-sm">
          <Link className="font-semibold text-primary hover:underline" to="/login">
            {zh ? "返回登录" : "Back to sign in"}
          </Link>
        </div>
      </AuthCard>
    </AuthFrame>
  );
}

function ResetResult({
  icon: Icon,
  title,
  description,
  actionLabel,
  onAction,
  secondaryLabel,
  onSecondary,
}: {
  icon: LucideIcon;
  title: string;
  description: string;
  actionLabel: string;
  onAction: () => void;
  secondaryLabel?: string;
  onSecondary?: () => void;
}) {
  return (
    <AuthFrame>
      <AuthCard>
        <span className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] bg-blue-50 text-primary dark:bg-blue-950/50">
          <Icon aria-hidden="true" size={21} />
        </span>
        <h1 className="mt-5 text-[27px] font-semibold">{title}</h1>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">{description}</p>
        <div className="mt-6 grid gap-3">
          <Button className="h-11 w-full" onClick={onAction}>{actionLabel}</Button>
          {secondaryLabel && onSecondary ? (
            <Button className="h-11 w-full" variant="ghost" onClick={onSecondary}>{secondaryLabel}</Button>
          ) : null}
        </div>
      </AuthCard>
    </AuthFrame>
  );
}

function resetTokenFromHash(hash: string): string | null {
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const token = params.get("token")?.trim() ?? "";
  return token && token.length <= 2048 ? token : null;
}
