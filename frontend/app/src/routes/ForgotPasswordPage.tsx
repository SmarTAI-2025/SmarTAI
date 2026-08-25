import { Loader2, MailQuestion } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useRequestPasswordReset } from "@/api/hooks";
import { AuthCard, AuthError, AuthFrame } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";
import { localizedPasswordResetError } from "@/lib/authErrors";

export function ForgotPasswordPage() {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const requestReset = useRequestPasswordReset();
  const [email, setEmail] = useState("");
  const [requested, setRequested] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    try {
      await requestReset.mutateAsync({ email: email.trim() });
      setRequested(true);
    } catch (error) {
      setFormError(localizedPasswordResetError(error, locale));
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
            <p className="mt-0.5 text-xs text-muted-foreground">{zh ? "使用注册邮箱" : "Use your registered email"}</p>
          </div>
        </div>
        <h1 className="mt-5 text-[27px] font-semibold">{zh ? "忘记密码" : "Forgot password"}</h1>
        {requested ? (
          <p className="mt-5 rounded-[8px] border border-blue-200 bg-blue-50 p-4 text-sm leading-6 text-blue-900 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-100">
            {zh
              ? "如果该邮箱对应 SmarTAI 账号，我们会发送密码重置链接。请检查邮箱。"
              : "If the email belongs to a SmarTAI account, we will send a password reset link. Check your inbox."}
          </p>
        ) : (
          <form className="mt-6 grid gap-4" onSubmit={handleSubmit}>
            <p className="text-sm leading-6 text-muted-foreground">
              {zh ? "输入注册时使用的科大邮箱，我们会发送一个 30 分钟内有效的重置链接。" : "Enter your registered USTC email. The reset link is valid for 30 minutes."}
            </p>
            <Field label={zh ? "科大邮箱" : "USTC email"}>
              <Input className="h-11 w-full" autoComplete="email" maxLength={320} required type="email" value={email} onChange={(event) => setEmail(event.target.value)} />
            </Field>
            {formError ? <AuthError message={formError} /> : null}
            <Button type="submit" className="h-11 w-full" disabled={requestReset.isPending}>
              {requestReset.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}
              {requestReset.isPending ? (zh ? "正在发送…" : "Sending…") : (zh ? "发送重置邮件" : "Send reset email")}
            </Button>
          </form>
        )}
        <div className="mt-5 border-t pt-5 text-center text-sm">
          <Link className="font-semibold text-primary hover:underline" to="/login">{zh ? "返回登录" : "Back to sign in"}</Link>
        </div>
      </AuthCard>
    </AuthFrame>
  );
}
