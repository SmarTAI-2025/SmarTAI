import { KeyRound, Loader2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useLocation } from "react-router-dom";
import { useConfirmPasswordReset } from "@/api/hooks";
import { AuthCard, AuthError, AuthFrame, AuthPasswordInput } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { useI18n } from "@/i18n/I18nProvider";
import { localizedPasswordResetError } from "@/lib/authErrors";

export function ResetPasswordPage() {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const location = useLocation();
  const confirmReset = useConfirmPasswordReset();
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const token = new URLSearchParams(location.hash.replace(/^#/, "")).get("token") ?? "";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    if (password !== confirmation) {
      setFormError(zh ? "两次输入的密码不一致。" : "The passwords do not match.");
      return;
    }
    try {
      await confirmReset.mutateAsync({ token, newPassword: password });
      setConfirmed(true);
    } catch (error) {
      setFormError(localizedPasswordResetError(error, locale));
    }
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
            <p className="mt-0.5 text-xs text-muted-foreground">{zh ? "设置新密码" : "Set a new password"}</p>
          </div>
        </div>
        <h1 className="mt-5 text-[27px] font-semibold">{zh ? "重置密码" : "Reset password"}</h1>
        {confirmed ? (
          <p className="mt-5 text-sm leading-6">{zh ? "密码已重置。请使用新密码登录。" : "Your password has been reset. Sign in with the new password."}</p>
        ) : !token ? (
          <AuthError message={zh ? "重置链接无效，请重新申请。" : "This reset link is invalid. Request a new one."} />
        ) : (
          <form className="mt-6 grid gap-3.5" onSubmit={handleSubmit}>
            <Field label={zh ? "新密码" : "New password"}>
              <AuthPasswordInput autoComplete="new-password" minLength={8} maxLength={128} required value={password} onChange={(event) => setPassword(event.target.value)} showLabel={zh ? "显示密码" : "Show password"} hideLabel={zh ? "隐藏密码" : "Hide password"} />
            </Field>
            <Field label={zh ? "确认新密码" : "Confirm new password"}>
              <AuthPasswordInput autoComplete="new-password" minLength={8} maxLength={128} required value={confirmation} onChange={(event) => setConfirmation(event.target.value)} showLabel={zh ? "显示确认密码" : "Show confirmation password"} hideLabel={zh ? "隐藏确认密码" : "Hide confirmation password"} />
            </Field>
            {formError ? <AuthError message={formError} /> : null}
            <Button type="submit" className="mt-1 h-11 w-full" disabled={confirmReset.isPending}>
              {confirmReset.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}
              {confirmReset.isPending ? (zh ? "正在重置…" : "Resetting…") : (zh ? "重置密码" : "Reset password")}
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
