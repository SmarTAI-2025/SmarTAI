import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

type HeaderTone = "primary" | "success" | "warning" | "danger";

const iconTone: Record<HeaderTone, string> = {
  primary: "bg-blue-50 text-primary dark:bg-blue-950/50",
  success: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300",
  warning: "bg-amber-50 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300",
  danger: "bg-rose-50 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300",
};

export function AuthFlowHeader({
  icon: Icon,
  eyebrow,
  helper,
  title,
  description,
  steps,
  stepsLabel,
  currentStep,
  tone = "primary",
}: {
  icon: LucideIcon;
  eyebrow: string;
  helper: string;
  title: string;
  description: string;
  steps: readonly string[];
  stepsLabel: string;
  currentStep: number;
  tone?: HeaderTone;
}) {
  return (
    <>
      <div className="flex items-center gap-3">
        <span className={cn("inline-flex h-10 w-10 items-center justify-center rounded-[10px]", iconTone[tone])}>
          <Icon aria-hidden="true" size={21} />
        </span>
        <div>
          <p className="text-xs font-semibold text-primary">{eyebrow}</p>
          <p className="mt-0.5 text-xs text-muted-foreground">{helper}</p>
        </div>
      </div>

      <ol aria-label={stepsLabel} className="mt-5 grid grid-cols-3 gap-2">
        {steps.map((step, index) => {
          const stepNumber = index + 1;
          const reached = stepNumber <= currentStep;
          return (
            <li key={step} aria-current={stepNumber === currentStep ? "step" : undefined}>
              <span className={cn("block h-1 rounded-full", reached ? "bg-primary" : "bg-muted")} />
              <span className={cn("mt-1.5 block text-[10px] font-medium leading-4 sm:text-[11px]", reached ? "text-foreground" : "text-muted-foreground")}>
                {step}
              </span>
            </li>
          );
        })}
      </ol>

      <h1 className="mt-5 text-[27px] font-semibold tracking-[-0.025em]">{title}</h1>
      <p className="mt-2 text-sm leading-6 text-muted-foreground">{description}</p>
    </>
  );
}
