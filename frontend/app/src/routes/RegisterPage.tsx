import { Loader2, MailCheck } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useRequestRegistration } from "@/api/hooks/registration";
import { AuthFlowHeader } from "@/components/auth/AuthFlowHeader";
import {
  AuthCard,
  AuthError,
  AuthFrame,
  AuthPasswordInput,
} from "@/components/auth/AuthFrame";
import { Button } from "@/components/ui/Button";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";
import { localizedRegistrationRequestError } from "@/lib/registrationErrors";
import {
  createPendingRegistrationFlow,
  savePendingRegistrationFlow,
} from "@/lib/registrationFlow";
import { registrationText, type RegistrationCopyKey } from "@/lib/registrationCopy";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function RegisterPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { locale } = useI18n();
  const text = (key: RegistrationCopyKey) => registrationText(locale, key);
  const draft = registrationDraft(location.state);
  const requestRegistration = useRequestRegistration();
  const [username, setUsername] = useState(draft?.username ?? "");
  const [email, setEmail] = useState(draft?.email ?? "");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const steps = [text("stepDetails"), text("stepEmail"), text("stepLogin")];

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const normalizedUsername = username.trim();
    const normalizedEmail = email.trim().toLowerCase();

    const validationError = validateRegistration({
      username: normalizedUsername,
      email: normalizedEmail,
      password,
      confirmation,
    }, text);
    if (validationError) {
      setFormError(validationError);
      return;
    }

    try {
      const request = {
        username: normalizedUsername,
        email: normalizedEmail,
        password,
      };
      const response = await requestRegistration.mutateAsync(request);
      const flow = createPendingRegistrationFlow(request, response, response.transport);
      savePendingRegistrationFlow(flow);
      setPassword("");
      setConfirmation("");
      navigate("/register/check-email", { replace: true });
    } catch (error) {
      setPassword("");
      setConfirmation("");
      setFormError(localizedRegistrationRequestError(error, locale));
    }
  }

  return (
    <AuthFrame>
      <AuthCard>
        <AuthFlowHeader
          icon={MailCheck}
          eyebrow={text("registerEyebrow")}
          helper={text("registerHelper")}
          title={text("registerTitle")}
          description={text("registerDescription")}
          stepsLabel={text("stepsLabel")}
          steps={steps}
          currentStep={1}
        />

        <form className="mt-6 grid gap-4" onSubmit={handleSubmit}>
          <Field label={text("usernameLabel")}>
            <Input
              className="h-11 w-full"
              autoComplete="username"
              autoFocus
              disabled={requestRegistration.isPending}
              minLength={3}
              maxLength={64}
              onChange={(event) => setUsername(event.target.value)}
              placeholder={text("usernamePlaceholder")}
              required
              value={username}
            />
          </Field>
          <Field label={text("emailLabel")} hint={text("emailHint")}>
            <Input
              className="h-11 w-full"
              autoComplete="email"
              disabled={requestRegistration.isPending}
              maxLength={254}
              onChange={(event) => setEmail(event.target.value)}
              placeholder={text("emailPlaceholder")}
              required
              type="email"
              value={email}
            />
          </Field>
          <Field label={text("passwordLabel")}>
            <AuthPasswordInput
              autoComplete="new-password"
              disabled={requestRegistration.isPending}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={text("passwordPlaceholder")}
              value={password}
              showLabel={text("showPassword")}
              hideLabel={text("hidePassword")}
              minLength={8}
              maxLength={128}
            />
          </Field>
          <Field label={text("confirmationLabel")}>
            <AuthPasswordInput
              autoComplete="new-password"
              disabled={requestRegistration.isPending}
              onChange={(event) => setConfirmation(event.target.value)}
              placeholder={text("confirmationPlaceholder")}
              value={confirmation}
              showLabel={text("showConfirmation")}
              hideLabel={text("hideConfirmation")}
              minLength={8}
              maxLength={128}
            />
          </Field>

          {formError ? <AuthError message={formError} /> : null}

          <Button
            type="submit"
            className="mt-1 h-11 w-full"
            disabled={requestRegistration.isPending}
          >
            {requestRegistration.isPending
              ? <Loader2 aria-hidden="true" className="animate-spin" size={16} />
              : <MailCheck aria-hidden="true" size={16} />}
            {requestRegistration.isPending ? text("sendingLink") : text("sendLink")}
          </Button>
        </form>

        <div className="mt-6 border-t pt-5 text-center text-sm text-muted-foreground">
          {text("alreadyRegistered")} {" "}
          <Link
            className="font-semibold text-primary outline-none hover:underline focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
            to="/login"
          >
            {text("backToLogin")}
          </Link>
        </div>
      </AuthCard>
    </AuthFrame>
  );
}

function validateRegistration(
  values: { username: string; email: string; password: string; confirmation: string },
  text: (key: RegistrationCopyKey) => string,
): string | null {
  if (values.username.length < 3) return text("usernameTooShort");
  if (!EMAIL_PATTERN.test(values.email) || values.email.length > 254) return text("invalidEmail");
  if (values.password.length < 8) return text("passwordTooShort");
  if (values.password.length > 128) return text("passwordTooLong");
  if (values.password !== values.confirmation) return text("passwordsMismatch");
  return null;
}

function registrationDraft(state: unknown): { username: string; email: string } | null {
  if (!state || typeof state !== "object" || !("registrationDraft" in state)) return null;
  const draft = (state as { registrationDraft?: unknown }).registrationDraft;
  if (!draft || typeof draft !== "object") return null;
  const values = draft as Record<string, unknown>;
  if (typeof values.username !== "string" || typeof values.email !== "string") return null;
  return { username: values.username, email: values.email };
}
