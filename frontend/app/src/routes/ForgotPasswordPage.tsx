import { Loader2, MailQuestion } from "lucide-react";
import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useRequestPasswordReset } from "@/api/hooks";
import { AuthCard, AuthError, AuthFrame } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";
import { localizedPasswordResetRequestError } from "@/lib/authErrors";
import { formatCooldown, rateLimitDelay, useRetryCooldown } from "@/hooks/useRetryCooldown";
import {
  createPasswordResetRequestMarker,
  savePasswordResetRequestMarker,
} from "@/lib/passwordResetRequestFlow";

export function ForgotPasswordPage() {
  const navigate = useNavigate();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const requestReset = useRequestPasswordReset();
  const cooldown = useRetryCooldown();
  const submitting = useRef(false);
  const [email, setEmail] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current || requestReset.isPending || cooldown.active) return;
    setFormError(null);
    submitting.current = true;
    try {
      const response = await requestReset.mutateAsync({ email: email.trim().toLowerCase() });
      savePasswordResetRequestMarker(createPasswordResetRequestMarker(response));
      requestReset.reset();
      setEmail("");
      navigate("/forgot-password/check-email", { replace: true });
    } catch (error) {
      requestReset.reset();
      cooldown.start(rateLimitDelay(error, "password_reset_rate_limited"));
      setFormError(localizedPasswordResetRequestError(error, locale));
    } finally {
      submitting.current = false;
    }
  }

  return (
    <AuthFrame>
      <AuthCard>
        <div className="flex items-center gap-3">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] bg-blue-50 text-primary dark:bg-blue-950/50">
            <MailQuestion aria-hidden="true" size={21} />
          </span>
          <div>
            <p className="text-xs font-semibold text-primary">{zh ? "找回账号" : "Account recovery"}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">{zh ? "匿名中性请求" : "Private neutral request"}</p>
          </div>
        </div>
        <h1 className="mt-5 text-[27px] font-semibold">{zh ? "忘记密码" : "Forgot password"}</h1>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {zh
            ? "输入注册邮箱。如果账号符合条件，我们会发送一个 30 分钟内有效的重置链接。"
            : "Enter your registration email. If the account is eligible, we send a reset link valid for 30 minutes."}
        </p>
        <form className="mt-6 grid gap-4" onSubmit={handleSubmit}>
          <Field label={zh ? "学校邮箱" : "School email"}>
            <Input
              aria-label={zh ? "学校邮箱" : "School email"}
              className="h-11 w-full"
              autoComplete="email"
              disabled={requestReset.isPending}
              maxLength={254}
              required
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>
          {formError ? <AuthError message={formError} /> : null}
          <Button type="submit" className="h-11 w-full" disabled={requestReset.isPending || cooldown.active}>
            {requestReset.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}
            {requestReset.isPending
              ? (zh ? "正在发送…" : "Sending…")
              : cooldown.active
                ? (zh ? `${formatCooldown(cooldown.seconds)} 后可重试` : `Retry in ${formatCooldown(cooldown.seconds)}`)
                : (zh ? "发送重置邮件" : "Send reset email")}
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
