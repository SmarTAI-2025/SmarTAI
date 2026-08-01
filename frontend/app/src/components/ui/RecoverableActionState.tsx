import {
  AlertTriangle,
  Clock3,
  FileWarning,
  KeyRound,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Locale } from "@/i18n/messages";
import type { RecoverableErrorInfo } from "@/lib/taskActionGuards";
import { cn } from "@/lib/cn";

export interface RecoveryAction {
  label: string;
  href?: string;
  state?: unknown;
  onClick?: () => void;
  busy?: boolean;
  disabled?: boolean;
}

export function RecoverableActionState({
  info,
  primaryAction,
  secondaryAction,
  compact = false,
  locale = "zh-CN",
  className,
}: {
  info: RecoverableErrorInfo;
  primaryAction?: RecoveryAction;
  secondaryAction?: RecoveryAction;
  compact?: boolean;
  locale?: Locale;
  className?: string;
}) {
  const primary = primaryAction ?? (info.actionHref
    ? { label: info.actionLabel, href: info.actionHref }
    : undefined);
  const Icon = info.actionKind === "byok"
    ? KeyRound
    : info.actionKind === "reupload" || info.actionKind === "reselect"
      ? FileWarning
      : info.actionKind === "retry" || info.actionKind === "refresh"
        ? RefreshCw
        : AlertTriangle;

  return (
    <section
      role="alert"
      className={cn(
        "rounded-[10px] border bg-card",
        compact ? "px-4 py-4" : "px-6 py-7 sm:px-8 sm:py-8",
        className,
      )}
    >
      <div className={cn("flex gap-4", compact ? "items-start" : "items-start sm:gap-5")}>
        <span className={cn(
          "inline-flex shrink-0 items-center justify-center rounded-full",
          compact ? "h-9 w-9" : "h-11 w-11",
          info.tone === "primary" && "bg-blue-50 text-primary dark:bg-blue-950/50",
          info.tone === "warning" && "bg-amber-50 text-amber-600 dark:bg-amber-950/40 dark:text-amber-300",
          info.tone === "danger" && "bg-red-50 text-danger dark:bg-red-950/40",
        )}>
          <Icon aria-hidden="true" className={compact ? "h-4 w-4" : "h-5 w-5"} />
        </span>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className={cn("font-bold text-foreground", compact ? "text-sm" : "text-xl")}>{info.title}</h2>
            {info.retryAfterSeconds !== undefined ? (
              <span className="inline-flex min-h-6 items-center gap-1 rounded-full bg-amber-50 px-2.5 text-[11px] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-200">
                <Clock3 aria-hidden="true" className="h-3 w-3" />
                {formatRetryAfter(info.retryAfterSeconds, locale)}
              </span>
            ) : null}
          </div>
          <p className={cn("text-muted-foreground", compact ? "mt-1 text-xs leading-5" : "mt-2 max-w-2xl text-sm leading-6")}>
            {info.description}
          </p>

          {primary || secondaryAction ? (
            <div className={cn("flex flex-col gap-2 sm:flex-row sm:flex-wrap", compact ? "mt-4" : "mt-6")}>
              {primary ? <ActionControl action={primary} primary /> : null}
              {secondaryAction ? <ActionControl action={secondaryAction} /> : null}
            </div>
          ) : null}

          {info.technicalDetails.length ? (
            <details className={cn("group", compact ? "mt-3" : "mt-5")}>
              <summary className="w-fit cursor-pointer select-none text-xs font-medium text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
                {locale === "en-US" ? "Technical details" : "技术信息"}
              </summary>
              <dl className="mt-2 grid gap-x-5 gap-y-1.5 rounded-[8px] bg-slate-50 px-3 py-2 text-[11px] dark:bg-slate-950/40 sm:grid-cols-2">
                {info.technicalDetails.map((item) => (
                  <div key={`${item.label}:${item.value}`} className="grid grid-cols-[max-content_minmax(0,1fr)] gap-2">
                    <dt className="text-muted-foreground">{item.label}</dt>
                    <dd className="min-w-0 break-all font-mono text-foreground">{item.value}</dd>
                  </div>
                ))}
              </dl>
            </details>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function ActionControl({ action, primary = false }: { action: RecoveryAction; primary?: boolean }) {
  const content: ReactNode = (
    <>
      {action.busy ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : null}
      {action.label}
    </>
  );
  const className = cn(
    "inline-flex h-10 items-center justify-center gap-2 rounded-[8px] px-4 text-sm font-semibold outline-none transition focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
    primary ? "bg-primary text-primary-foreground hover:opacity-90" : "border bg-card text-foreground hover:bg-muted",
    (action.disabled || action.busy) && "pointer-events-none opacity-50",
  );
  if (action.href) {
    return <Link to={action.href} state={action.state} aria-disabled={action.disabled || action.busy || undefined} className={className}>{content}</Link>;
  }
  return <button type="button" disabled={action.disabled || action.busy} onClick={action.onClick} className={className}>{content}</button>;
}

function formatRetryAfter(seconds: number, locale: Locale): string {
  if (seconds < 60) return locale === "en-US" ? `Retry in ${seconds}s` : `${seconds} 秒后可重试`;
  return locale === "en-US" ? `Retry in about ${Math.ceil(seconds / 60)}m` : `约 ${Math.ceil(seconds / 60)} 分钟后可重试`;
}
