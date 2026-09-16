import { CheckCircle2, Clock3, Link2Off, Loader2, ShieldCheck, type LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useVerifyRegistration } from "@/api/hooks/registration";
import { AuthFlowHeader } from "@/components/auth/AuthFlowHeader";
import { AuthCard, AuthError, AuthFrame } from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { InlineNotice } from "@/components/ui/InlineNotice";
import { useI18n } from "@/i18n/I18nProvider";
import { verificationLinkFailure, type VerificationLinkFailure } from "@/lib/registrationErrors";
import { clearPendingRegistrationFlow } from "@/lib/registrationFlow";
import { registrationText, type RegistrationCopyKey } from "@/lib/registrationCopy";

type VerifyState = "ready" | "success" | VerificationLinkFailure;

export function RegisterVerifyPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const { locale } = useI18n();
  const text = (key: RegistrationCopyKey) => registrationText(locale, key);
  const incomingToken = verificationTokenFromHash(location.hash);
  const [token, setToken] = useState(incomingToken);
  const [state, setState] = useState<VerifyState>(token ? "ready" : "invalid");
  const verifyRegistration = useVerifyRegistration();
  const steps = [text("stepDetails"), text("stepEmail"), text("stepLogin")];

  useEffect(() => {
    if (!incomingToken || incomingToken === token) return;
    setToken(incomingToken);
    setState("ready");
  }, [incomingToken, token]);

  async function handleVerify() {
    if (!token || verifyRegistration.isPending) return;
    try {
      const response = await verifyRegistration.mutateAsync({ token });
      clearPendingRegistrationFlow();
      setState(response.status === "already_verified" ? "used" : "success");
      stripTokenFromAddress(navigate);
    } catch (error) {
      const failure = verificationLinkFailure(error);
      setState(failure);
      if (failure !== "network") stripTokenFromAddress(navigate);
    }
  }

  if (state === "success") {
    return (
      <RegistrationResult
        icon={CheckCircle2}
        tone="accent"
        eyebrow={text("successEyebrow")}
        helper={text("successHelper")}
        title={text("successTitle")}
        description={text("successDescription")}
        steps={steps}
        stepsLabel={text("stepsLabel")}
        actionLabel={text("goToLogin")}
        onAction={() => navigate("/login", { replace: true })}
      />
    );
  }

  if (state === "expired") {
    return (
      <RegistrationResult
        icon={Clock3}
        tone="warning"
        eyebrow={text("expiredEyebrow")}
        helper={text("expiredHelper")}
        title={text("expiredTitle")}
        description={text("expiredDescription")}
        steps={steps}
        stepsLabel={text("stepsLabel")}
        actionLabel={text("restartRegistration")}
        onAction={() => navigate("/register", { replace: true })}
        secondaryLabel={text("backToEmailPage")}
        onSecondary={() => navigate("/register/check-email")}
      />
    );
  }

  if (state === "used") {
    return (
      <RegistrationResult
        icon={CheckCircle2}
        tone="accent"
        eyebrow={text("usedEyebrow")}
        helper={text("usedHelper")}
        title={text("usedTitle")}
        description={text("usedDescription")}
        steps={steps}
        stepsLabel={text("stepsLabel")}
        actionLabel={text("goToLogin")}
        onAction={() => navigate("/login", { replace: true })}
        secondaryLabel={text("restartRegistration")}
        onSecondary={() => navigate("/register")}
      />
    );
  }

  if (state === "invalid") {
    return (
      <RegistrationResult
        icon={Link2Off}
        tone="danger"
        eyebrow={text("invalidEyebrow")}
        helper={text("invalidHelper")}
        title={text("invalidTitle")}
        description={text("invalidDescription")}
        steps={steps}
        stepsLabel={text("stepsLabel")}
        actionLabel={text("restartRegistration")}
        onAction={() => navigate("/register", { replace: true })}
      />
    );
  }

  return (
    <AuthFrame>
      <AuthCard>
        <AuthFlowHeader
          icon={ShieldCheck}
          eyebrow={text("verifyEyebrow")}
          helper={text("verifyHelper")}
          title={text("verifyTitle")}
          description={text("verifyDescription")}
          stepsLabel={text("stepsLabel")}
          steps={steps}
          currentStep={2}
        />

        <InlineNotice tone="neutral" className="mt-5">
          {text("verifySecurityNote")}
        </InlineNotice>

        {state === "network" ? (
          <div className="mt-4"><AuthError message={text("verifyNetworkError")} /></div>
        ) : null}

        <Button
          className="mt-5 h-11 w-full"
          disabled={verifyRegistration.isPending}
          onClick={handleVerify}
        >
          {verifyRegistration.isPending
            ? <Loader2 aria-hidden="true" className="animate-spin" size={16} />
            : <ShieldCheck aria-hidden="true" size={16} />}
          {verifyRegistration.isPending
            ? text("confirmingRegistration")
            : state === "network"
              ? text("verifyRetry")
              : text("confirmRegistration")}
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
  tone: "accent" | "warning" | "danger";
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
          stepsLabel={stepsLabel}
          steps={steps}
          currentStep={tone === "accent" ? 3 : 2}
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

function stripTokenFromAddress(navigate: ReturnType<typeof useNavigate>): void {
  navigate("/register/verify", { replace: true });
}
