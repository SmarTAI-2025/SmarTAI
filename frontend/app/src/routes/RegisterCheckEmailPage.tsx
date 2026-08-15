import { ArrowRight, Check, Clock3, Inbox, Loader2, RotateCw } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { normalizeAPIError } from "@/api/client";
import {
  getDevelopmentVerificationPreview,
  type DevelopmentRegistrationPreview,
} from "@/api/registration";
import { useResendRegistration } from "@/api/hooks/registration";
import { AuthFlowHeader } from "@/components/auth/AuthFlowHeader";
import { AuthCard, AuthError, AuthFrame } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { InlineNotice } from "@/components/ui/InlineNotice";
import { useI18n } from "@/i18n/I18nProvider";
import { localizedRegistrationRequestError } from "@/lib/registrationErrors";
import {
  clearPendingRegistrationFlow,
  readPendingRegistrationFlow,
  savePendingRegistrationFlow,
  updatePendingRegistrationFlow,
} from "@/lib/registrationFlow";
import { registrationText, type RegistrationCopyKey } from "@/lib/registrationCopy";
import type { PendingRegistrationFlow } from "@/types/registration";

export function RegisterCheckEmailPage() {
  const navigate = useNavigate();
  const { locale } = useI18n();
  const text = (key: RegistrationCopyKey, values?: Record<string, string | number>) => (
    registrationText(locale, key, values)
  );
  const resendRegistration = useResendRegistration();
  const [flow, setFlow] = useState<PendingRegistrationFlow | null>(() => readPendingRegistrationFlow());
  const [now, setNow] = useState(Date.now());
  const [error, setError] = useState<string | null>(null);
  const [resent, setResent] = useState(false);
  const [developmentPreview, setDevelopmentPreview] = useState<DevelopmentRegistrationPreview | null>(null);
  const steps = [text("stepDetails"), text("stepEmail"), text("stepLogin")];

  useEffect(() => {
    if (!flow) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [Boolean(flow)]);

  useEffect(() => {
    let active = true;
    if (!flow || flow.transport !== "development_mock") {
      setDevelopmentPreview(null);
      return () => { active = false; };
    }
    void getDevelopmentVerificationPreview(flow.requestId, locale).then((preview) => {
      if (active) setDevelopmentPreview(preview);
    });
    return () => { active = false; };
  }, [flow, locale]);

  const secondsUntilExpiry = Math.max(0, Math.ceil(((flow?.expiresAt ?? now) - now) / 1000));
  const secondsUntilResend = Math.max(0, Math.ceil(((flow?.resendAvailableAt ?? now) - now) / 1000));

  if (!flow) {
    return (
      <AuthFrame>
        <AuthCard>
          <AuthFlowHeader
            icon={Clock3}
            eyebrow={text("missingFlowTitle")}
            helper={text("invalidHelper")}
            title={text("missingFlowTitle")}
            description={text("missingFlowDescription")}
            stepsLabel={text("stepsLabel")}
            steps={steps}
            currentStep={2}
            tone="warning"
          />
          <Button className="mt-6 h-11 w-full" onClick={() => navigate("/register", { replace: true })}>
            {text("restartRegistration")}
          </Button>
        </AuthCard>
      </AuthFrame>
    );
  }

  async function handleResend() {
    if (!flow || secondsUntilResend > 0 || resendRegistration.isPending) return;
    setError(null);
    setResent(false);
    try {
      const response = await resendRegistration.mutateAsync({ request_id: flow.requestId });
      const updated = updatePendingRegistrationFlow(flow, response);
      savePendingRegistrationFlow(updated);
      setFlow(updated);
      setNow(Date.now());
      setResent(true);
    } catch (nextError) {
      const retryAfterSeconds = normalizeAPIError(nextError).retryAfterSeconds;
      if (retryAfterSeconds) {
        const updated = {
          ...flow,
          resendAvailableAt: Math.max(flow.resendAvailableAt, Date.now() + retryAfterSeconds * 1000),
        };
        savePendingRegistrationFlow(updated);
        setFlow(updated);
      }
      setError(localizedRegistrationRequestError(nextError, locale));
    }
  }

  function handleEdit() {
    clearPendingRegistrationFlow();
    navigate("/register", {
      replace: true,
      state: { registrationDraft: { username: flow?.username ?? "", email: flow?.email ?? "" } },
    });
  }

  return (
    <AuthFrame>
      <AuthCard>
        <AuthFlowHeader
          icon={Inbox}
          eyebrow={text("checkEyebrow")}
          helper={text("checkHelper")}
          title={text("checkTitle")}
          description={text("checkDescription", { email: flow.email })}
          stepsLabel={text("stepsLabel")}
          steps={steps}
          currentStep={2}
        />

        <div className="mt-5 rounded-[10px] border bg-muted/30 p-4">
          <ol className="grid gap-3 text-sm leading-6">
            {[text("checkStepInbox"), text("checkStepSpam"), text("checkStepLink")].map((item) => (
              <li key={item} className="flex gap-2.5">
                <span className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/10 text-primary">
                  <Check aria-hidden="true" size={12} strokeWidth={3} />
                </span>
                <span>{item}</span>
              </li>
            ))}
          </ol>
        </div>

        <p className="mt-4 text-center text-sm font-medium text-foreground">
          {secondsUntilExpiry > 0
            ? text("expiresIn", { time: formatDuration(secondsUntilExpiry) })
            : text("linkExpired")}
        </p>
        <p className="mt-1 text-center text-xs leading-5 text-muted-foreground">{text("checkPrivacy")}</p>

        {resent ? (
          <InlineNotice tone="success" title={text("resentTitle")} className="mt-4" >
            {text("resentDescription")}
          </InlineNotice>
        ) : null}
        {error ? <div className="mt-4"><AuthError message={error} /></div> : null}

        {developmentPreview ? (
          <InlineNotice
            tone="info"
            title={developmentPreview.title}
            className="mt-4"
            action={(
              <Link
                to={developmentPreview.path}
                className="inline-flex h-8 items-center gap-1.5 rounded-md border border-primary/30 bg-card px-2.5 text-xs font-semibold text-primary outline-none hover:bg-primary/5 focus-visible:ring-2 focus-visible:ring-ring"
              >
                {developmentPreview.actionLabel}
                <ArrowRight aria-hidden="true" size={13} />
              </Link>
            )}
          >
            {developmentPreview.description}
          </InlineNotice>
        ) : null}

        <div className="mt-5 grid gap-3">
          <Button
            className="h-11 w-full"
            variant="secondary"
            disabled={secondsUntilResend > 0 || resendRegistration.isPending}
            onClick={handleResend}
          >
            {resendRegistration.isPending
              ? <Loader2 aria-hidden="true" className="animate-spin" size={16} />
              : <RotateCw aria-hidden="true" size={16} />}
            {resendRegistration.isPending
              ? text("resendingLink")
              : secondsUntilResend > 0
                ? text("resendIn", { time: formatDuration(secondsUntilResend) })
                : text("resendLink")}
          </Button>
          <Button className="h-11 w-full" variant="ghost" onClick={handleEdit}>
            {text("changeDetails")}
          </Button>
        </div>
      </AuthCard>
    </AuthFrame>
  );
}

function formatDuration(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}
