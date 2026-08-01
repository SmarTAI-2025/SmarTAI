import { LoaderCircle } from "lucide-react";
import { useEffect, useId, useRef } from "react";

interface UnsavedChangesDialogProps {
  title: string;
  description: string;
  stayLabel: string;
  leaveLabel: string;
  saveLabel?: string;
  savingLabel?: string;
  saving?: boolean;
  saveError?: string;
  onStay: () => void;
  onLeave: () => void;
  onSave?: () => void;
}

export function UnsavedChangesDialog({
  title,
  description,
  stayLabel,
  leaveLabel,
  saveLabel,
  savingLabel,
  saving = false,
  saveError,
  onStay,
  onLeave,
  onSave,
}: UnsavedChangesDialogProps) {
  const dialogRef = useRef<HTMLElement>(null);
  const stayButtonRef = useRef<HTMLButtonElement>(null);
  const onStayRef = useRef(onStay);
  const onLeaveRef = useRef(onLeave);
  const onSaveRef = useRef(onSave);
  const savingRef = useRef(saving);
  const titleId = useId();
  const descriptionId = useId();

  onStayRef.current = onStay;
  onLeaveRef.current = onLeave;
  onSaveRef.current = onSave;
  savingRef.current = saving;

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => stayButtonRef.current?.focus());

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (savingRef.current) return;
        onStayRef.current();
        return;
      }
      if (event.key === "Tab" && savingRef.current) {
        event.preventDefault();
        dialogRef.current?.focus();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
        "button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (document.activeElement === dialogRef.current) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKeyDown);
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
  }, []);

  useEffect(() => {
    if (!saving) return;
    const frame = window.requestAnimationFrame(() => dialogRef.current?.focus());
    return () => window.cancelAnimationFrame(frame);
  }, [saving]);

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-slate-950/40 p-4" role="presentation">
      <section
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-busy={saving || undefined}
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
        className="w-full max-w-md rounded-[10px] border bg-card p-5 shadow-2xl"
      >
        <h2 id={titleId} className="text-lg font-bold text-foreground">{title}</h2>
        <p id={descriptionId} className="mt-2 text-sm leading-6 text-muted-foreground">{description}</p>
        {saveError ? (
          <p role="alert" className="mt-3 rounded-[7px] border border-red-200 bg-red-50 px-3 py-2 text-xs font-medium text-red-700 dark:border-red-900/60 dark:bg-red-950/40 dark:text-red-200">
            {saveError}
          </p>
        ) : null}
        <div className="mt-5 flex flex-wrap justify-end gap-2">
          <button
            ref={stayButtonRef}
            type="button"
            disabled={saving}
            className="h-9 rounded-[7px] border bg-card px-4 text-sm font-semibold outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => onStayRef.current()}
          >
            {stayLabel}
          </button>
          <button
            type="button"
            disabled={saving}
            className="h-9 rounded-[7px] bg-danger px-4 text-sm font-semibold text-white outline-none hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => onLeaveRef.current()}
          >
            {leaveLabel}
          </button>
          {saveLabel && onSave ? (
            <button
              type="button"
              disabled={saving}
              className="inline-flex h-9 items-center gap-1.5 rounded-[7px] bg-primary px-4 text-sm font-semibold text-primary-foreground outline-none hover:opacity-90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => onSaveRef.current?.()}
            >
              {saving ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : null}
              {saving ? savingLabel ?? saveLabel : saveLabel}
            </button>
          ) : null}
        </div>
      </section>
    </div>
  );
}
