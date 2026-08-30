import { CheckCircle2, Clock3, Link2Off, Loader2, ShieldCheck, type LucideIcon } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useVerifyRegistration } from "@/api/hooks";
import { AuthFlowHeader } from "@/components/auth/AuthFlowHeader";
import { AuthCard, AuthError, AuthFrame } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { InlineNotice } from "@/components/ui/InlineNotice";
import { useI18n } from "@/i18n/I18nProvider";
import { formatCooldown, rateLimitDelay, useRetryCooldown } from "@/hooks/useRetryCooldown";
import {
  localizedRegistrationVerifyError,
  registrationLinkFailure,
  type LinkFailure,
} from "@/lib/authErrors";
import { clearPendingRegistrationFlow } from "@/lib/registrationFlow";

type VerifyState = "ready" | "success" | LinkFailure;

export function RegisterVerifyPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const verifyRegistration = useVerifyRegistration();
  const cooldown = useRetryCooldown();
  const tokenRef = useRef<string | null | undefined>(undefined);
  if (tokenRef.current === undefined) tokenRef.current = verificationTokenFromHash(location.hash);
  const submitting = useRef(false);
  const [state, setState] = useState<VerifyState>(tokenRef.current ? "ready" : "invalid");
  const [retryError, setRetryError] = useState<string | null>(null);
  const [alreadyVerified, setAlreadyVerified] = useState(false);
  const steps = zh ? ["填写信息", "验证邮箱", "登录使用"] : ["Account details", "Verify email", "Sign in"];

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
  }, []); // Extract once, then remove the fragment before the user interacts.

  async function handleVerify() {
    if (!tokenRef.current || submitting.current || verifyRegistration.isPending || cooldown.active) return;
    submitting.current = true;
    setRetryError(null);
    const token = tokenRef.current;
    tokenRef.current = null;
    try {
      const response = await verifyRegistration.mutateAsync(token);
      verifyRegistration.reset();
      clearPendingRegistrationFlow();
      setAlreadyVerified(response.status === "already_verified");
      setState("success");
    } catch (error) {
      verifyRegistration.reset();
      const failure = registrationLinkFailure(error);
      if (failure === "retry") tokenRef.current = token;
      cooldown.start(rateLimitDelay(error, "registration_rate_limited"));
      setRetryError(failure === "retry" ? localizedRegistrationVerifyError(error, locale) : null);
      setState(failure);
    } finally {
      submitting.current = false;
    }
  }

  if (state === "success") {
    return (
      <RegistrationResult
        icon={CheckCircle2}
        tone="success"
        eyebrow={zh ? "邮箱验证完成" : "Email verified"}
        helper={alreadyVerified ? (zh ? "该流程此前已确认" : "This flow was already confirmed") : (zh ? "账号已安全创建" : "Account created securely")}
        title={zh ? "注册完成" : "Registration complete"}
        description={zh
          ? "现在可以使用用户名和密码登录；验证流程不会自动登录。"
          : "You can now sign in with your username and password. Verification does not sign you in automatically."}
        steps={steps}
        stepsLabel={zh ? "注册进度" : "Registration progress"}
        actionLabel={zh ? "前往登录" : "Continue to sign in"}
        onAction={() => navigate("/login", { replace: true })}
      />
    );
  }

  if (state === "expired") {
    return (
      <RegistrationResult
        icon={Clock3}
        tone="warning"
        eyebrow={zh ? "链接已过期" : "Link expired"}
        helper={zh ? "30 分钟有效期已结束" : "The 30-minute validity period ended"}
        title={zh ? "需要新的验证链接" : "Request a new verification link"}
        description={zh ? "请返回注册页重新申请；旧链接不会被再次使用。" : "Start registration again. The old link cannot be reused."}
        steps={steps}
        stepsLabel={zh ? "注册进度" : "Registration progress"}
        actionLabel={zh ? "重新注册" : "Start again"}
        onAction={() => navigate("/register", { replace: true })}
      />
    );
  }

  if (state === "used") {
    return (
      <RegistrationResult
        icon={CheckCircle2}
        tone="warning"
        eyebrow={zh ? "链接已使用" : "Link already used"}
        helper={zh ? "一次性链接不能重复使用" : "Single-use links cannot be reused"}
        title={zh ? "此链接不可再用" : "This link cannot be used again"}
        description={zh ? "如果账号已创建请直接登录；否则重新开始注册。" : "Sign in if the account was created; otherwise start registration again."}
        steps={steps}
        stepsLabel={zh ? "注册进度" : "Registration progress"}
        actionLabel={zh ? "前往登录" : "Continue to sign in"}
        onAction={() => navigate("/login", { replace: true })}
        secondaryLabel={zh ? "重新注册" : "Start again"}
        onSecondary={() => navigate("/register", { replace: true })}
      />
    );
  }

  if (state === "invalid") {
    return (
      <RegistrationResult
        icon={Link2Off}
        tone="danger"
        eyebrow={zh ? "链接不可用" : "Link unavailable"}
        helper={zh ? "链接缺失或已损坏" : "The link is missing or damaged"}
        title={zh ? "无法验证此链接" : "We cannot verify this link"}
        description={zh ? "请确认链接复制完整，或重新注册获取新邮件。" : "Check that the complete link was copied, or register again for a new email."}
        steps={steps}
        stepsLabel={zh ? "注册进度" : "Registration progress"}
        actionLabel={zh ? "重新注册" : "Start again"}
        onAction={() => navigate("/register", { replace: true })}
      />
    );
  }

  return (
    <AuthFrame>
      <AuthCard>
        <AuthFlowHeader
          icon={ShieldCheck}
          eyebrow={zh ? "确认学校邮箱" : "Confirm school email"}
          helper={zh ? "一次性安全链接" : "Single-use secure link"}
          title={zh ? "完成邮箱验证" : "Complete email verification"}
          description={zh
            ? "确认后才创建普通教师账号。仅打开页面不会消费链接。"
            : "A teacher account is created only after confirmation. Merely opening this page does not consume the link."}
          steps={steps}
          stepsLabel={zh ? "注册进度" : "Registration progress"}
          currentStep={2}
        />
        <InlineNotice tone="neutral" className="mt-5">
          {zh ? "验证完成后仍需手动登录，不会自动进入工作台。" : "After verification, you still sign in manually; this flow never opens the workspace automatically."}
        </InlineNotice>
        {retryError ? <div className="mt-4"><AuthError message={retryError} /></div> : null}
        <Button className="mt-5 h-11 w-full" disabled={verifyRegistration.isPending || cooldown.active} onClick={handleVerify}>
          {verifyRegistration.isPending
            ? <Loader2 aria-hidden="true" className="animate-spin" size={16} />
            : <ShieldCheck aria-hidden="true" size={16} />}
          {verifyRegistration.isPending
            ? (zh ? "正在确认…" : "Confirming…")
            : cooldown.active
              ? (zh ? `${formatCooldown(cooldown.seconds)} 后可重试` : `Retry in ${formatCooldown(cooldown.seconds)}`)
              : state === "retry"
              ? (zh ? "重试验证" : "Retry verification")
              : (zh ? "确认并完成注册" : "Confirm registration")}
        </Button>
      </AuthCard>
    </AuthFrame>
  );
}

function RegistrationResult({
  icon,
  tone,
  eyebrow,
  helper,
  title,
  description,
  steps,
  stepsLabel,
  actionLabel,
  onAction,
  secondaryLabel,
  onSecondary,
}: {
  icon: LucideIcon;
  tone: "success" | "warning" | "danger";
  eyebrow: string;
  helper: string;
  title: string;
  description: string;
  steps: readonly string[];
  stepsLabel: string;
  actionLabel: string;
  onAction: () => void;
  secondaryLabel?: string;
  onSecondary?: () => void;
}) {
  return (
    <AuthFrame>
      <AuthCard>
        <AuthFlowHeader
          icon={icon}
          tone={tone}
          eyebrow={eyebrow}
          helper={helper}
          title={title}
          description={description}
          steps={steps}
          stepsLabel={stepsLabel}
          currentStep={tone === "success" ? 3 : 2}
        />
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

function verificationTokenFromHash(hash: string): string | null {
  const params = new URLSearchParams(hash.startsWith("#") ? hash.slice(1) : hash);
  const token = params.get("token")?.trim() ?? "";
  return token && token.length <= 2048 ? token : null;
}
