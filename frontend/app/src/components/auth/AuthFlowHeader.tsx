import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/cn";

type HeaderTone = "primary" | "accent" | "warning" | "danger";

const toneClasses: Record<HeaderTone, string> = {
  primary: "bg-primary/5 text-primary",
  accent: "bg-accent/10 text-accent",
  warning: "bg-warning/10 text-warning",
  danger: "bg-danger/10 text-danger",
};

const eyebrowToneClasses: Record<HeaderTone, string> = {
  primary: "text-primary",
  accent: "text-accent",
  warning: "text-warning",
  danger: "text-danger",
};

export function AuthFlowHeader({
  icon: Icon,
  eyebrow,
  helper,
  title,
  description,
  stepsLabel,
  steps,
  currentStep,
  tone = "primary",
}: {
  icon: LucideIcon;
  eyebrow: string;
  helper: string;
  title: string;
  description: string;
  stepsLabel: string;
  steps: readonly string[];
  currentStep: number;
  tone?: HeaderTone;
}) {
  return (
    <>
      <div className="flex items-center gap-3">
        <span className={cn("inline-flex h-10 w-10 items-center justify-center rounded-[10px]", toneClasses[tone])}>
          <Icon aria-hidden="true" size={21} />
        </span>
        <div>
          <p className={cn("text-xs font-semibold", eyebrowToneClasses[tone])}>
            {eyebrow}
          </p>
          <p className="mt-0.5 text-xs text-muted-foreground">{helper}</p>
        </div>
      </div>

      <ol aria-label={stepsLabel} className="mt-5 grid grid-cols-3 gap-2">
        {steps.map((step, index) => {
          const number = index + 1;
          const reached = number <= currentStep;
          return (
            <li key={step} aria-current={number === currentStep ? "step" : undefined}>
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
