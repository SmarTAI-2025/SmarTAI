import {
  Atom,
  Bot,
  BrainCircuit,
  Cpu,
  Feather,
  Moon,
  Orbit,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { providerDisplayName } from "@/lib/modelPresentation";

const PROVIDER_ICONS: Record<string, { icon: LucideIcon; className: string }> = {
  anthropic: {
    icon: Feather,
    className: "bg-amber-50 text-amber-700 dark:bg-amber-950/35 dark:text-amber-300",
  },
  gemini: {
    icon: Sparkles,
    className: "bg-blue-50 text-blue-600 dark:bg-blue-950/35 dark:text-blue-300",
  },
  openai: {
    icon: Atom,
    className: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/35 dark:text-emerald-300",
  },
  zhipu: {
    icon: BrainCircuit,
    className: "bg-indigo-50 text-indigo-600 dark:bg-indigo-950/35 dark:text-indigo-300",
  },
  deepseek: {
    icon: Cpu,
    className: "bg-violet-50 text-violet-700 dark:bg-violet-950/35 dark:text-violet-300",
  },
  moonshot: {
    icon: Moon,
    className: "bg-cyan-50 text-cyan-700 dark:bg-cyan-950/35 dark:text-cyan-300",
  },
  qwen: {
    icon: Orbit,
    className: "bg-rose-50 text-rose-700 dark:bg-rose-950/35 dark:text-rose-300",
  },
};

export function ProviderIcon({
  providerType,
  className,
}: {
  providerType: string;
  className?: string;
}) {
  const provider = providerType.trim().toLowerCase();
  const presentation = PROVIDER_ICONS[provider] ?? {
    icon: Bot,
    className: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  };
  const Icon = presentation.icon;

  return (
    <span
      aria-hidden="true"
      data-provider-icon={provider || "unknown"}
      title={providerDisplayName(providerType)}
      className={cn(
        "inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-[9px]",
        presentation.className,
        className,
      )}
    >
      <Icon className="h-[19px] w-[19px]" strokeWidth={1.9} />
    </span>
  );
}
