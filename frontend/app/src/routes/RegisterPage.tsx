import { Loader2, MailCheck } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useRequestRegistration } from "@/api/hooks";
import { AuthCard, AuthError, AuthFrame, AuthPasswordInput } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";
import { localizedAuthError } from "@/lib/authErrors";

export function RegisterPage() {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const requestRegistration = useRequestRegistration();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [requested, setRequested] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    if (password !== confirmation) {
      setFormError(zh ? "两次输入的密码不一致。" : "The passwords do not match.");
      return;
    }
    try {
      await requestRegistration.mutateAsync({ username: username.trim(), email: email.trim(), password });
      setRequested(true);
    } catch (error) {
      setFormError(localizedAuthError(error, locale, "register"));
    }
  }

  return (
    <AuthFrame>
      <AuthCard>
        <div className="flex items-center gap-3"><span className="inline-flex h-10 w-10 items-center justify-center rounded-[10px] bg-blue-50 text-primary dark:bg-blue-950/50"><MailCheck aria-hidden="true" size={21} /></span><div><p className="text-xs font-semibold text-primary">{zh ? "邮箱验证注册" : "Email verification"}</p><p className="mt-0.5 text-xs text-muted-foreground">{zh ? "仅限科大邮箱" : "USTC email required"}</p></div></div>
        <h1 className="mt-5 text-[27px] font-semibold tracking-[-0.025em]">{zh ? "创建教师账号" : "Create teacher account"}</h1>
        {requested ? <div className="mt-6 rounded-[8px] border border-blue-200 bg-blue-50 p-4 text-sm leading-6 text-blue-900 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-100">{zh ? "验证邮件已发送。请打开邮件中的链接，并在确认页面点击按钮完成注册。" : "Verification email sent. Open its link and click the confirmation button to finish registration."}</div> : <form className="mt-6 grid gap-3.5" onSubmit={handleSubmit}><Field label={zh ? "用户名" : "Username"}><Input className="h-11 w-full" autoComplete="username" minLength={3} maxLength={64} required value={username} onChange={(event) => setUsername(event.target.value)} /></Field><Field label={zh ? "科大邮箱" : "USTC email"}><Input className="h-11 w-full" autoComplete="email" maxLength={320} required type="email" value={email} onChange={(event) => setEmail(event.target.value)} /></Field><div className="grid gap-3.5 sm:grid-cols-2"><Field label={zh ? "设置密码" : "Password"}><AuthPasswordInput autoComplete="new-password" minLength={8} maxLength={128} required value={password} onChange={(event) => setPassword(event.target.value)} showLabel={zh ? "显示密码" : "Show password"} hideLabel={zh ? "隐藏密码" : "Hide password"} /></Field><Field label={zh ? "确认密码" : "Confirm password"}><AuthPasswordInput autoComplete="new-password" minLength={8} maxLength={128} required value={confirmation} onChange={(event) => setConfirmation(event.target.value)} showLabel={zh ? "显示确认密码" : "Show confirmation password"} hideLabel={zh ? "隐藏确认密码" : "Hide confirmation password"} /></Field></div>{formError ? <AuthError message={formError} /> : null}<Button type="submit" className="mt-1 h-11 w-full" disabled={requestRegistration.isPending}>{requestRegistration.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}{requestRegistration.isPending ? (zh ? "正在发送…" : "Sending…") : (zh ? "发送验证邮件" : "Send verification email")}</Button></form>}
        <div className="mt-5 border-t pt-5 text-center text-sm text-muted-foreground">{zh ? "已有账号？" : "Already have an account?"}{" "}<Link className="font-semibold text-primary hover:underline" to="/login">{zh ? "返回登录" : "Back to sign in"}</Link></div>
      </AuthCard>
    </AuthFrame>
  );
}
