import { Loader2, MailCheck } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { getAPIErrorCode } from "@/api/client";
import { useRequestRegistration, useResendRegistration } from "@/api/hooks";
import { AuthCard, AuthError, AuthFrame, AuthPasswordInput } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";
import { localizedAuthError } from "@/lib/authErrors";
import type { EmailRegistrationResponse } from "@/types";

interface PendingRegistration {
  requestId: string;
  resendDeadline: number;
}

export function RegisterPage() {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const requestRegistration = useRequestRegistration();
  const resendRegistration = useResendRegistration();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [registration, setRegistration] = useState<PendingRegistration | null>(null);
  const [cooldownSeconds, setCooldownSeconds] = useState(0);

  useEffect(() => {
    if (!registration || registration.resendDeadline <= performance.now()) {
      setCooldownSeconds(0);
      return;
    }

    const updateCountdown = () => {
      setCooldownSeconds(Math.max(0, Math.ceil((registration.resendDeadline - performance.now()) / 1000)));
    };
    updateCountdown();
    const intervalId = window.setInterval(updateCountdown, 250);
    return () => window.clearInterval(intervalId);
  }, [registration]);

  function retainRegistration(response: EmailRegistrationResponse) {
    const resendAfterSeconds = Math.max(0, response.resend_after_seconds);
    setRegistration({
      requestId: response.request_id,
      resendDeadline: performance.now() + resendAfterSeconds * 1000,
    });
    setCooldownSeconds(resendAfterSeconds);
  }

  function registrationError(error: unknown) {
    if (getAPIErrorCode(error) === "registration_rate_limited") {
      return zh ? "请求过于频繁，请稍后再试。" : "Too many requests. Try again later.";
    }
    return localizedAuthError(error, locale, "register");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    if (password !== confirmation) {
      setFormError(zh ? "两次输入的密码不一致。" : "The passwords do not match.");
      return;
    }
    try {
      const response = await requestRegistration.mutateAsync({
        username: username.trim(),
        email: email.trim(),
        password,
      });
      retainRegistration(response);
    } catch (error) {
      setFormError(registrationError(error));
    }
  }

  async function handleResend() {
    if (!registration || cooldownSeconds > 0) return;
    setFormError(null);
    try {
      retainRegistration(await resendRegistration.mutateAsync(registration.requestId));
    } catch (error) {
      setFormError(registrationError(error));
    }
  }

  const resendLabel = resendRegistration.isPending
    ? (zh ? "正在重新发送…" : "Resending…")
    : cooldownSeconds > 0
      ? (zh ? `重新发送验证邮件（${cooldownSeconds} 秒）` : `Resend verification email (${cooldownSeconds}s)`)
      : (zh ? "重新发送验证邮件" : "Resend verification email");

  return (
    <AuthFrame>
      <AuthCard>
        <div className="flex items-center gap-3">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] bg-blue-50 text-primary dark:bg-blue-950/50">
            <MailCheck aria-hidden="true" size={21} />
          </span>
          <div>
            <p className="text-xs font-semibold text-primary">{zh ? "邮箱验证注册" : "Email verification"}</p>
            <p className="mt-0.5 text-xs text-muted-foreground">{zh ? "仅限科大邮箱" : "USTC email required"}</p>
          </div>
        </div>
        <h1 className="mt-5 text-[27px] font-semibold tracking-[-0.025em]">{zh ? "创建教师账号" : "Create teacher account"}</h1>
        {registration ? (
          <div className="mt-6 grid gap-4">
            <div className="rounded-[8px] border border-blue-200 bg-blue-50 p-4 text-sm leading-6 text-blue-900 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-100">
              {zh
                ? "验证邮件已发送。请打开邮件中的链接，并在确认页面点击按钮完成注册。"
                : "Verification email sent. Open its link and click the confirmation button to finish registration."}
            </div>
            {formError ? <AuthError message={formError} /> : null}
            <Button
              type="button"
              variant="secondary"
              className="h-11 w-full"
              disabled={cooldownSeconds > 0 || resendRegistration.isPending}
              onClick={handleResend}
            >
              {resendRegistration.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}
              {resendLabel}
            </Button>
          </div>
        ) : (
          <form className="mt-6 grid gap-3.5" onSubmit={handleSubmit}>
            <Field label={zh ? "用户名" : "Username"}>
              <Input className="h-11 w-full" autoComplete="username" minLength={3} maxLength={64} required value={username} onChange={(event) => setUsername(event.target.value)} />
            </Field>
            <Field label={zh ? "科大邮箱" : "USTC email"}>
              <Input className="h-11 w-full" autoComplete="email" maxLength={320} required type="email" value={email} onChange={(event) => setEmail(event.target.value)} />
            </Field>
            <div className="grid gap-3.5 sm:grid-cols-2">
              <Field label={zh ? "设置密码" : "Password"}>
                <AuthPasswordInput autoComplete="new-password" minLength={8} maxLength={128} required value={password} onChange={(event) => setPassword(event.target.value)} showLabel={zh ? "显示密码" : "Show password"} hideLabel={zh ? "隐藏密码" : "Hide password"} />
              </Field>
              <Field label={zh ? "确认密码" : "Confirm password"}>
                <AuthPasswordInput autoComplete="new-password" minLength={8} maxLength={128} required value={confirmation} onChange={(event) => setConfirmation(event.target.value)} showLabel={zh ? "显示确认密码" : "Show confirmation password"} hideLabel={zh ? "隐藏确认密码" : "Hide confirmation password"} />
              </Field>
            </div>
            {formError ? <AuthError message={formError} /> : null}
            <Button type="submit" className="mt-1 h-11 w-full" disabled={requestRegistration.isPending}>
              {requestRegistration.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}
              {requestRegistration.isPending ? (zh ? "正在发送…" : "Sending…") : (zh ? "发送验证邮件" : "Send verification email")}
            </Button>
          </form>
        )}
        <div className="mt-5 border-t pt-5 text-center text-sm text-muted-foreground">
          {zh ? "已有账号？" : "Already have an account?"}{" "}
          <Link className="font-semibold text-primary hover:underline" to="/login">{zh ? "返回登录" : "Back to sign in"}</Link>
        </div>
      </AuthCard>
    </AuthFrame>
  );
}
