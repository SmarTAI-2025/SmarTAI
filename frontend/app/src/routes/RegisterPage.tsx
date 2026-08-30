import { Loader2, MailCheck } from "lucide-react";
import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useRequestRegistration } from "@/api/hooks";
import { AuthFlowHeader } from "@/components/auth/AuthFlowHeader";
import { AuthCard, AuthError, AuthFrame, AuthPasswordInput } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";
import { formatCooldown, rateLimitDelay, useRetryCooldown } from "@/hooks/useRetryCooldown";
import { localizedRegistrationRequestError } from "@/lib/authErrors";
import { createPendingRegistrationFlow, savePendingRegistrationFlow } from "@/lib/registrationFlow";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function RegisterPage() {
  const navigate = useNavigate();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const requestRegistration = useRequestRegistration();
  const cooldown = useRetryCooldown();
  const submitting = useRef(false);
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const steps = zh ? ["填写信息", "验证邮箱", "登录使用"] : ["Account details", "Verify email", "Sign in"];

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current || requestRegistration.isPending || cooldown.active) return;
    setFormError(null);

    const normalizedUsername = username.trim();
    const normalizedEmail = email.trim().toLowerCase();
    const validationError = validateRegistration(
      { username: normalizedUsername, email: normalizedEmail, password, confirmation },
      zh,
    );
    if (validationError) {
      setFormError(validationError);
      return;
    }

    submitting.current = true;
    try {
      const response = await requestRegistration.mutateAsync({
        username: normalizedUsername,
        email: normalizedEmail,
        password,
      });
      const flow = createPendingRegistrationFlow(normalizedEmail, response);
      requestRegistration.reset();
      setPassword("");
      setConfirmation("");
      setUsername("");
      setEmail("");
      savePendingRegistrationFlow(flow);
      navigate("/register/check-email", { replace: true });
    } catch (error) {
      requestRegistration.reset();
      cooldown.start(rateLimitDelay(error, "registration_rate_limited"));
      setPassword("");
      setConfirmation("");
      setFormError(localizedRegistrationRequestError(error, locale));
    } finally {
      submitting.current = false;
    }
  }

  return (
    <AuthFrame>
      <AuthCard>
        <AuthFlowHeader
          icon={MailCheck}
          eyebrow={zh ? "学校邮箱验证" : "School email verification"}
          helper={zh ? "链接 30 分钟内有效" : "Link valid for 30 minutes"}
          title={zh ? "创建教师账号" : "Create teacher account"}
          description={zh
            ? "填写账号信息。我们会向学校邮箱发送一次性链接，验证完成后才创建普通教师账号。"
            : "Enter your account details. We create a teacher account only after the one-time school-email link is confirmed."}
          steps={steps}
          stepsLabel={zh ? "注册进度" : "Registration progress"}
          currentStep={1}
        />

        <form className="mt-6 grid gap-3.5" onSubmit={handleSubmit}>
          <Field label={zh ? "用户名" : "Username"}>
            <Input
              aria-label={zh ? "用户名" : "Username"}
              className="h-11 w-full"
              autoComplete="username"
              disabled={requestRegistration.isPending}
              minLength={3}
              maxLength={64}
              placeholder={zh ? "至少 3 个字符" : "At least 3 characters"}
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </Field>
          <Field label={zh ? "学校邮箱" : "School email"}>
            <Input
              aria-label={zh ? "学校邮箱" : "School email"}
              className="h-11 w-full"
              autoComplete="email"
              disabled={requestRegistration.isPending}
              maxLength={254}
              placeholder="name@ustc.edu.cn"
              required
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
            <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
              {zh
                ? "首期支持 ustc.edu.cn 及其点边界子域名；最终资格以服务端校验为准。"
                : "The first cohort uses ustc.edu.cn and its dot-boundary subdomains; the server makes the final eligibility decision."}
            </p>
          </Field>
          <div className="grid gap-3.5 sm:grid-cols-2">
            <Field label={zh ? "设置密码" : "Password"}>
              <AuthPasswordInput
                autoComplete="new-password"
                disabled={requestRegistration.isPending}
                minLength={8}
                maxLength={128}
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                showLabel={zh ? "显示密码" : "Show password"}
                hideLabel={zh ? "隐藏密码" : "Hide password"}
              />
            </Field>
            <Field label={zh ? "确认密码" : "Confirm password"}>
              <AuthPasswordInput
                autoComplete="new-password"
                disabled={requestRegistration.isPending}
                minLength={8}
                maxLength={128}
                required
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                showLabel={zh ? "显示确认密码" : "Show confirmation password"}
                hideLabel={zh ? "隐藏确认密码" : "Hide confirmation password"}
              />
            </Field>
          </div>
          <p className="text-xs leading-5 text-muted-foreground">
            {zh ? "密码需 8–128 个字符；验证邮件成功确认前不会创建账号。" : "Use 8–128 characters. No account is created until the email is confirmed."}
          </p>
          {formError ? <AuthError message={formError} /> : null}
          <Button type="submit" className="mt-1 h-11 w-full" disabled={requestRegistration.isPending || cooldown.active}>
            {requestRegistration.isPending ? <Loader2 aria-hidden="true" className="animate-spin" size={16} /> : null}
            {requestRegistration.isPending
              ? (zh ? "正在发送…" : "Sending…")
              : cooldown.active
                ? (zh ? `${formatCooldown(cooldown.seconds)} 后可重试` : `Retry in ${formatCooldown(cooldown.seconds)}`)
                : (zh ? "发送验证链接" : "Send verification link")}
          </Button>
        </form>

        <div className="mt-5 border-t pt-5 text-center text-sm text-muted-foreground">
          {zh ? "已有账号？" : "Already have an account?"}{" "}
          <Link className="font-semibold text-primary hover:underline" to="/login">
            {zh ? "返回登录" : "Back to sign in"}
          </Link>
        </div>
      </AuthCard>
    </AuthFrame>
  );
}

function validateRegistration(
  values: { username: string; email: string; password: string; confirmation: string },
  zh: boolean,
): string | null {
  if (values.username.length < 3) return zh ? "用户名至少需要 3 个字符。" : "Username must contain at least 3 characters.";
  if (values.username.length > 64) return zh ? "用户名不能超过 64 个字符。" : "Username cannot exceed 64 characters.";
  if (!EMAIL_PATTERN.test(values.email) || values.email.length > 254) return zh ? "请输入有效的学校邮箱地址。" : "Enter a valid school email address.";
  if (values.password.length < 8) return zh ? "密码至少需要 8 个字符。" : "Password must contain at least 8 characters.";
  if (values.password.length > 128) return zh ? "密码不能超过 128 个字符。" : "Password cannot exceed 128 characters.";
  if (values.password !== values.confirmation) return zh ? "两次输入的密码不一致。" : "The passwords do not match.";
  return null;
}
