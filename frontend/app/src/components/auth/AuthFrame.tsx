import { Eye, EyeOff } from "lucide-react";
import { useState, type ChangeEventHandler, type ReactNode } from "react";
import { LanguageToggle } from "@/components/layout/LanguageToggle";
import { Input } from "@/components/ui/Input";
import { useI18n } from "@/i18n/I18nProvider";

export function AuthFrame({ children }: { children: ReactNode }) {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  return (
    <main className="relative min-h-screen overflow-hidden bg-[#f7f9fc] text-foreground dark:bg-[#0b1220]">
      <AuthBackdrop />
      <header className="absolute inset-x-0 top-0 z-20">
        <div className="mx-auto flex h-[70px] w-full max-w-[1440px] items-center justify-between px-5 sm:px-10">
          <div className="flex items-baseline gap-3">
            <span className="text-[22px] font-bold tracking-[-0.03em] text-primary">SmarTAI</span>
            <span className="hidden text-xs font-medium text-muted-foreground sm:inline">
              {zh ? "SmarTAI 智能批改工作台" : "SmarTAI Intelligent Grading Workspace"}
            </span>
          </div>
          <LanguageToggle className="h-8 rounded-md border-slate-200 bg-white/75 backdrop-blur-sm dark:border-slate-700 dark:bg-slate-900/70" />
        </div>
      </header>

      <div className="relative z-10 mx-auto flex min-h-screen w-full max-w-[1440px] items-center justify-center px-5 py-24 sm:px-8">
        {children}
      </div>
    </main>
  );
}

export function AuthCard({ children }: { children: ReactNode }) {
  return (
    <section className="w-full max-w-[440px] rounded-[12px] border border-slate-200/90 bg-white/95 p-6 shadow-[0_18px_55px_rgba(15,23,42,0.08)] backdrop-blur-sm dark:border-slate-700 dark:bg-slate-900/95 sm:p-8">
      {children}
    </section>
  );
}

export function AuthPasswordInput({
  value,
  disabled,
  autoComplete,
  placeholder,
  onChange,
  showLabel,
  hideLabel,
}: {
  value: string;
  disabled?: boolean;
  autoComplete: string;
  placeholder: string;
  onChange: ChangeEventHandler<HTMLInputElement>;
  showLabel: string;
  hideLabel: string;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="relative">
      <Input
        className="h-11 w-full pr-11"
        autoComplete={autoComplete}
        disabled={disabled}
        onChange={onChange}
        placeholder={placeholder}
        required
        type={visible ? "text" : "password"}
        value={value}
      />
      <button
        type="button"
        className="absolute right-1.5 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        disabled={disabled}
        onClick={() => setVisible((current) => !current)}
        aria-label={visible ? hideLabel : showLabel}
      >
        {visible ? <EyeOff aria-hidden="true" size={16} /> : <Eye aria-hidden="true" size={16} />}
      </button>
    </div>
  );
}

export function AuthError({ message }: { message: string }) {
  return (
    <div
      role="alert"
      aria-live="polite"
      className="rounded-[8px] border border-rose-200 bg-rose-50 px-3.5 py-3 text-sm leading-5 text-rose-700 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200"
    >
      {message}
    </div>
  );
}

function AuthBackdrop() {
  return (
    <svg
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 h-full w-full text-blue-500"
      viewBox="0 0 1440 900"
      preserveAspectRatio="xMidYMid slice"
      fill="none"
    >
      <defs>
        <pattern id="auth-dot-grid" width="28" height="28" patternUnits="userSpaceOnUse">
          <circle cx="1.5" cy="1.5" r="1.5" fill="currentColor" opacity="0.08" />
        </pattern>
      </defs>
      <rect width="1440" height="900" fill="url(#auth-dot-grid)" />
      <path
        d="M112 662C264 562 337 694 473 602C596 520 665 555 760 475"
        stroke="currentColor"
        strokeWidth="1.5"
        opacity="0.1"
        strokeDasharray="5 9"
      />
      <path
        d="M842 245C962 160 1080 202 1190 126C1260 78 1321 82 1380 105"
        stroke="currentColor"
        strokeWidth="1.5"
        opacity="0.1"
        strokeDasharray="5 9"
      />
      <g opacity="0.12" stroke="currentColor" strokeWidth="2">
        <rect x="88" y="180" width="168" height="214" rx="18" />
        <path d="M122 232H218M122 270H202M122 308H212" />
        <path d="m126 350 13 13 27-31" strokeWidth="4" />
      </g>
      <g opacity="0.12" stroke="currentColor" strokeWidth="2">
        <rect x="1174" y="500" width="180" height="224" rx="18" />
        <path d="M1210 553H1314M1210 591H1296M1210 629H1318" />
        <circle cx="1234" cy="675" r="17" />
        <path d="m1226 675 6 6 12-14" strokeWidth="3" />
      </g>
      <g fill="currentColor">
        <circle cx="301" cy="634" r="5" opacity="0.16" />
        <circle cx="760" cy="475" r="5" opacity="0.16" />
        <circle cx="1070" cy="190" r="5" opacity="0.16" />
        <circle cx="1190" cy="126" r="5" opacity="0.16" />
      </g>
    </svg>
  );
}
