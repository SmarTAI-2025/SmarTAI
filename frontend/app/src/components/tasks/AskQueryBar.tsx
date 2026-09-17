import { LoaderCircle, X } from "lucide-react";
import { useRef, type ReactNode } from "react";
import { SmarTAIMascot } from "@/components/brand/SmarTAIMascot";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import type { TaskFilterController } from "@/hooks/useTaskFilterIntent";
import { cn } from "@/lib/cn";
import { classifyRecoverableError } from "@/lib/taskActionGuards";

interface AskQueryBarProps {
  locale: string;
  value: string;
  onChange: (value: string) => void;
  onApply: (value: string) => void;
  onCancel?: () => void;
  pending?: boolean;
  placeholder: string;
  label?: string;
  className?: string;
  feedback?: ReactNode;
  onFocus?: () => void;
  onBlur?: () => void;
}

/** The same editable, IME-safe input and controls on every Ask surface. */
export function AskQueryBar({ locale, value, onChange, onApply, onCancel, pending = false, placeholder, label, className, feedback, onFocus, onBlur }: AskQueryBarProps) {
  const zh = locale === "zh-CN";
  const composing = useRef(false);
  const input = useImeSafeQuery({ value, onCommit: onChange, onDraftChange: onCancel });
  return (
    <div className={cn("min-w-0", className)} data-ask-smartai>
      <form role="search" className="flex min-w-0 items-center gap-2 rounded-[10px] border bg-card p-1.5" onSubmit={(event) => {
        event.preventDefault();
        if (!composing.current && !pending && input.draftValue.trim()) onApply(input.commitDraft());
      }}>
        <SmarTAIMascot variant={pending ? "grading" : "thinking"} size="xs" />
        <label className="relative min-w-0 flex-1">
          <span className="sr-only">{label ?? (zh ? "Ask SmarTAI：筛选与排序" : "Ask SmarTAI: filter and sort")}</span>
          <input
            type="text" inputMode="search" value={input.draftValue} maxLength={500}
            placeholder={placeholder}
            className="h-10 w-full min-w-0 rounded-[7px] bg-muted/30 pl-3 pr-9 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:ring-2 focus:ring-primary/20"
            onChange={input.handleChange}
            onCompositionStart={() => { composing.current = true; input.handleCompositionStart(); }}
            onCompositionEnd={(event) => { composing.current = false; input.handleCompositionEnd(event); }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && (composing.current || event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault();
            }}
            onFocus={onFocus}
            onBlur={(event) => { input.handleBlur(event); composing.current = false; onBlur?.(); }}
          />
          {input.draftValue ? <button type="button" aria-label={zh ? "清空查询" : "Clear query"}
            className="absolute right-1 top-1/2 inline-flex h-8 w-8 -translate-y-1/2 items-center justify-center rounded-full text-muted-foreground hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
            onMouseDown={(event) => event.preventDefault()} onClick={() => input.commitValue("")}>
            <X aria-hidden="true" className="h-4 w-4" />
          </button> : null}
        </label>
        <button type="submit" disabled={pending || !input.draftValue.trim()}
          className="inline-flex h-10 shrink-0 items-center justify-center gap-1.5 rounded-[7px] bg-primary px-3 text-xs font-semibold text-primary-foreground outline-none hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">
          {pending ? <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 animate-spin" /> : null}
          {pending ? (zh ? "理解中…" : "Interpreting…") : "Ask SmarTAI"}
        </button>
      </form>
      {feedback}
    </div>
  );
}

export function TaskQueryBar({ filter, taskId, ...props }: Omit<AskQueryBarProps, "value" | "onChange" | "onApply" | "onCancel" | "pending" | "feedback"> & { filter: TaskFilterController; taskId?: string }) {
  const zh = props.locale === "zh-CN";
  const locale = zh ? "zh-CN" : "en-US";
  const info = filter.error ? classifyRecoverableError(filter.error, {
    locale, phase: "analytics_filter_intent", returnTo: taskId ? `/tasks/${encodeURIComponent(taskId)}` : "/history",
  }) : null;
  const message = filter.pending ? (zh ? "正在理解筛选与排序…" : "Interpreting the filter and sort…")
    : filter.unrecognized ? ((zh ? "暂时无法完整理解此条件，未应用部分筛选。" : "The full instruction could not be understood; no partial filter was applied.") + (filter.explanation ? ` ${filter.explanation}` : ""))
      : filter.source === "local" ? (zh ? "已本地匹配，未调用模型。" : "Matched locally; no model call.")
        : filter.explanation;
  return <AskQueryBar {...props} value={filter.query} onChange={filter.setQuery} onApply={(value) => void filter.apply(value)} onCancel={filter.cancel} pending={filter.pending} feedback={
    info ? <RecoverableActionState compact locale={locale} info={info}
      primaryAction={info.actionKind === "byok" ? undefined : { label: info.actionLabel, onClick: () => void filter.apply() }} />
      : message ? <p role="status" className={cn("mt-2 text-xs leading-5", filter.unrecognized ? "text-amber-700" : "text-muted-foreground")}>{message}</p> : null
  } />;
}
