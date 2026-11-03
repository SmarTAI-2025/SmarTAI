import { Check, Clock3, Inbox, Loader2, RotateCw } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";
import { useResendRegistration } from "@/api/hooks";
import { AuthFlowHeader } from "@/components/auth/AuthFlowHeader";
import { AuthCard, AuthError, AuthFrame } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { InlineNotice } from "@/components/ui/InlineNotice";
import { useI18n } from "@/i18n/I18nProvider";
import { localizedRegistrationRequestError } from "@/lib/authErrors";
import {
  clearPendingRegistrationFlow,
  isCurrentPendingRegistrationFlow,
  readPendingRegistrationFlow,
  savePendingRegistrationFlow,
  updatePendingRegistrationFlow,
  type PendingRegistrationFlow,
} from "@/lib/registrationFlow";

export function RegisterCheckEmailPage() {
  const navigate = useNavigate();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const resendRegistration = useResendRegistration();
  const submitting = useRef(false);
  const active = useRef(false);
  const [flow, setFlow] = useState<PendingRegistrationFlow | null>(() => readPendingRegistrationFlow());
  const [now, setNow] = useState(Date.now());
  const [error, setError] = useState<string | null>(null);
  const [resent, setResent] = useState(false);
  const [recoveryReason, setRecoveryReason] = useState<"expired" | "invalid" | null>(null);
  const [inviteFailure, setInviteFailure] = useState<"required" | "invalid" | null>(null);
  const steps = zh ? ["填写信息", "验证邮箱", "登录使用"] : ["Account details", "Verify email", "Sign in"];

  useLayoutEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  useEffect(() => {
    if (!flow) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [flow]);

  if (!flow) {
    return (
      <AuthFrame>
        <AuthCard>
          <AuthFlowHeader
            icon={Clock3}
            tone="warning"
            eyebrow={recoveryReason === "expired"
              ? (zh ? "验证请求已过期" : "Verification request expired")
              : (zh ? "注册信息已失效" : "Registration details unavailable")}
            helper={zh ? "注册信息已失效" : "Registration details expired"}
            title={zh ? "请重新填写注册信息" : "Start registration again"}
            description={recoveryReason
              ? (zh
                  ? "原验证请求已过期或不可再用。请重新提交注册信息获取新邮件。"
                  : "The original verification request expired or is no longer usable. Submit registration again for a new email.")
              : (zh
                  ? "请重新填写注册信息，获取新的验证邮件。"
                  : "Please register again to receive a new verification email.")}
            steps={steps}
            stepsLabel={zh ? "注册进度" : "Registration progress"}
            currentStep={2}
          />
          <Button className="mt-6 h-11 w-full" onClick={() => navigate("/register", { replace: true })}>
            {zh ? "重新填写" : "Start again"}
          </Button>
        </AuthCard>
      </AuthFrame>
    );
  }

  const secondsUntilExpiry = Math.max(0, Math.ceil((flow.expiresAt - now) / 1000));
  const secondsUntilResend = Math.max(0, Math.ceil((flow.resendAvailableAt - now) / 1000));

  async function handleResend() {
    if (!active.current || !flow || inviteFailure || secondsUntilResend > 0 || submitting.current || resendRegistration.isPending) return;
    const requestId = flow.requestId;
    const isCurrent = () => active.current && isCurrentPendingRegistrationFlow(requestId);
    if (!isCurrent()) return;
    submitting.current = true;
    setError(null);
    setResent(false);
    try {
      const response = await resendRegistration.mutateAsync(requestId);
      if (!isCurrent()) return;
      const updated = updatePendingRegistrationFlow(flow, response);
      resendRegistration.reset();
      savePendingRegistrationFlow(updated);
      setFlow(updated);
      setNow(Date.now());
      setResent(true);
    } catch (nextError) {
      if (!isCurrent()) return;
      resendRegistration.reset();
      const code = getAPIErrorCode(nextError);
      if (code === "registration_invite_required" || code === "registration_invite_invalid") {
        clearPendingRegistrationFlow();
        setInviteFailure(code === "registration_invite_required" ? "required" : "invalid");
        return;
      }
      if (
        code === "verification_link_expired"
        || code === "verification_link_invalid"
        || code === "verification_link_already_used"
      ) {
        clearPendingRegistrationFlow();
        setRecoveryReason(code === "verification_link_expired" ? "expired" : "invalid");
        setFlow(null);
        return;
      }
      const retryAfterSeconds = normalizeAPIError(nextError).retryAfterSeconds;
      if (retryAfterSeconds !== undefined) {
        const updated = {
          ...flow,
          resendAvailableAt: Math.max(flow.resendAvailableAt, Date.now() + retryAfterSeconds * 1000),
        };
        savePendingRegistrationFlow(updated);
        setFlow(updated);
      }
      setError(localizedRegistrationRequestError(nextError, locale));
    } finally {
      submitting.current = false;
    }
  }

  function restartRegistration() {
    active.current = false;
    if (flow && isCurrentPendingRegistrationFlow(flow.requestId)) clearPendingRegistrationFlow();
    navigate("/register", { replace: true });
  }

  return (
    <AuthFrame>
      <AuthCard>
        <AuthFlowHeader
          icon={Inbox}
          eyebrow={zh ? "验证邮件已请求" : "Verification email requested"}
          helper={zh ? "链接约 30 分钟内有效" : "Link valid for about 30 minutes"}
          title={zh ? "请查收学校邮箱" : "Check your school email"}
          description={zh
            ? `邮件请求已由服务端接受，目标邮箱显示为 ${flow.maskedEmail}。`
            : `The server accepted the request for ${flow.maskedEmail}.`}
          steps={steps}
          stepsLabel={zh ? "注册进度" : "Registration progress"}
          currentStep={2}
        />

        <div className="mt-5 rounded-[10px] border bg-muted/30 p-4">
          <ol className="grid gap-3 text-sm leading-6">
            {[
              zh ? "打开收件箱，找到 SmarTAI 验证邮件。" : "Open your inbox and find the SmarTAI verification email.",
              zh ? "没有看到时，请检查垃圾邮件或推广分类。" : "If it is missing, check spam and promotions.",
              zh ? "打开最新邮件中的链接，再主动确认创建账号。" : "Open the newest link, then actively confirm account creation.",
            ].map((item) => (
              <li key={item} className="flex gap-2.5">
                <span className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
                  <Check aria-hidden="true" size={12} strokeWidth={3} />
                </span>
                <span>{item}</span>
              </li>
            ))}
          </ol>
        </div>

        <p className="mt-4 text-center text-sm font-medium">
          {secondsUntilExpiry > 0
            ? (zh ? `本次链接将在 ${formatDuration(secondsUntilExpiry)} 后失效` : `This link expires in ${formatDuration(secondsUntilExpiry)}`)
            : (zh ? "本次链接可能已到期，可重新发送新链接。" : "This link may have expired. You can request a new one.")}
        </p>

        {resent ? (
          <InlineNotice tone="success" title={zh ? "新链接已发送" : "New link sent"} className="mt-4">
            {zh ? "旧链接已失效，请只使用最新邮件。" : "The previous link is invalid. Use only the newest email."}
          </InlineNotice>
        ) : null}
        {error ? <div className="mt-4"><AuthError message={error} /></div> : null}

        <div className="mt-5 grid gap-3">
          <Button
            className="h-11 w-full"
            variant="secondary"
            disabled={Boolean(inviteFailure) || secondsUntilResend > 0 || resendRegistration.isPending}
            onClick={handleResend}
          >
            {resendRegistration.isPending
              ? <Loader2 aria-hidden="true" className="animate-spin" size={16} />
              : <RotateCw aria-hidden="true" size={16} />}
            {resendRegistration.isPending
              ? (zh ? "正在重新发送…" : "Resending…")
              : secondsUntilResend > 0
                ? (zh ? `${formatDuration(secondsUntilResend)} 后可重新发送` : `Resend in ${formatDuration(secondsUntilResend)}`)
                : (zh ? "重新发送验证链接" : "Resend verification link")}
          </Button>
          <Button className="h-11 w-full" variant="ghost" onClick={restartRegistration}>
            {zh ? "返回修改信息" : "Change account details"}
          </Button>
        </div>
      </AuthCard>
      {inviteFailure ? (
        <RegistrationInviteRecoveryDialog reason={inviteFailure} zh={zh} onReturn={restartRegistration} />
      ) : null}
    </AuthFrame>
  );
}

function RegistrationInviteRecoveryDialog({ reason, zh, onReturn }: {
  reason: "required" | "invalid";
  zh: boolean;
  onReturn: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  return (
    <dialog
      ref={dialog}
      role="alertdialog"
      aria-labelledby="registration-invite-title"
      aria-describedby="registration-invite-description"
      onCancel={(event) => { event.preventDefault(); onReturn(); }}
      className="fixed inset-0 m-auto w-[calc(100%-2rem)] max-w-md rounded-xl border bg-card p-6 text-foreground shadow-2xl backdrop:bg-slate-950/40"
    >
      <h2 id="registration-invite-title" className="text-lg font-semibold">
        {reason === "required"
          ? (zh ? "需要填写邀请码" : "Invitation code required")
          : (zh ? "邀请码已使用或已失效" : "Invitation code already used or no longer valid")}
      </h2>
      <p id="registration-invite-description" className="mt-3 text-sm leading-6 text-muted-foreground">
        {reason === "required"
          ? (zh ? "此注册请求缺少邀请码。请返回注册页填写邀请码，再获取新的验证邮件。" : "This registration request has no invitation code. Return to registration and enter one to request a new verification email.")
          : (zh ? "此邀请码已被使用或已失效，无法重新发送验证邮件。请返回注册页更换有效且未使用的邀请码。" : "This invitation code has already been used or is no longer valid, so the email cannot be resent. Return to registration and use a valid, unused code.")}
      </p>
      <div className="mt-5 flex justify-end">
        <Button autoFocus onClick={onReturn}>{zh ? "返回注册页" : "Return to registration"}</Button>
      </div>
    </dialog>
  );
}

function formatDuration(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}
