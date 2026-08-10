import { useEffect, useId, useRef, useState, type CSSProperties, type PointerEvent, type ReactNode } from "react";
import { cn } from "@/lib/cn";

const DEFAULT_RATIO = 50;
const MIN_RATIO = 35;
const MAX_RATIO = 65;
const HANDLE_WIDTH = 12;
const MIN_PREVIEW_WIDTH = 360;
const MIN_CONTENT_WIDTH = 480;

type SplitStyle = CSSProperties & {
  "--source-preview-share": string;
  "--source-content-share": string;
};

export function SourceComparisonWorkspace({
  open,
  preview,
  children,
  separatorLabel,
  className,
}: {
  open: boolean;
  preview: ReactNode;
  children: ReactNode;
  separatorLabel: string;
  className?: string;
}) {
  const workspaceRef = useRef<HTMLDivElement>(null);
  const ratioRef = useRef(DEFAULT_RATIO);
  const draggingRef = useRef(false);
  const previousCursorRef = useRef("");
  const previousUserSelectRef = useRef("");
  const [committedRatio, setCommittedRatio] = useState(DEFAULT_RATIO);
  const id = useId();
  const previewId = `${id}-original`;
  const contentId = `${id}-recognized`;

  useEffect(() => () => restoreDocumentInteraction(previousCursorRef.current, previousUserSelectRef.current), []);

  function applyRatio(nextRatio: number, commit: boolean) {
    const ratio = clampRatio(nextRatio);
    ratioRef.current = ratio;
    const workspace = workspaceRef.current;
    workspace?.style.setProperty("--source-preview-share", `${ratio}fr`);
    workspace?.style.setProperty("--source-content-share", `${100 - ratio}fr`);
    if (commit) setCommittedRatio(ratio);
  }

  function ratioFromPointer(clientX: number) {
    const rect = workspaceRef.current?.getBoundingClientRect();
    if (!rect) return ratioRef.current;
    const availableWidth = Math.max(1, rect.width - HANDLE_WIDTH);
    const rawRatio = ((clientX - rect.left) / availableWidth) * 100;
    const minimum = Math.max(MIN_RATIO, (MIN_PREVIEW_WIDTH / availableWidth) * 100);
    const maximum = Math.min(MAX_RATIO, 100 - (MIN_CONTENT_WIDTH / availableWidth) * 100);
    if (minimum > maximum) return DEFAULT_RATIO;
    return Math.min(maximum, Math.max(minimum, rawRatio));
  }

  function startDragging(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    draggingRef.current = true;
    event.currentTarget.setPointerCapture?.(event.pointerId);
    const root = document.documentElement;
    previousCursorRef.current = root.style.cursor;
    previousUserSelectRef.current = root.style.userSelect;
    root.style.cursor = "col-resize";
    root.style.userSelect = "none";
    applyRatio(ratioFromPointer(event.clientX), false);
  }

  function drag(event: PointerEvent<HTMLDivElement>) {
    if (!draggingRef.current) return;
    event.preventDefault();
    applyRatio(ratioFromPointer(event.clientX), false);
  }

  function finishDragging(event: PointerEvent<HTMLDivElement>) {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    restoreDocumentInteraction(previousCursorRef.current, previousUserSelectRef.current);
    setCommittedRatio(ratioRef.current);
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    let nextRatio: number | null = null;
    if (event.key === "ArrowLeft") nextRatio = ratioRef.current - (event.shiftKey ? 10 : 2);
    else if (event.key === "ArrowRight") nextRatio = ratioRef.current + (event.shiftKey ? 10 : 2);
    else if (event.key === "Home") nextRatio = MIN_RATIO;
    else if (event.key === "End") nextRatio = MAX_RATIO;
    if (nextRatio === null) return;
    event.preventDefault();
    event.stopPropagation();
    applyRatio(nextRatio, true);
  }

  const splitStyle: SplitStyle = {
    "--source-preview-share": `${committedRatio}fr`,
    "--source-content-share": `${100 - committedRatio}fr`,
  };

  return (
    <div
      ref={workspaceRef}
      className={cn(
        "min-w-0",
        open && "grid grid-cols-1 items-start gap-4 lg:[grid-template-columns:minmax(360px,var(--source-preview-share))_12px_minmax(480px,var(--source-content-share))] lg:gap-0",
        className,
      )}
      style={splitStyle}
      data-preview-open={open ? "true" : "false"}
    >
      <div id={previewId} className={cn("min-w-0", open ? "block" : "hidden")}>
        {open ? preview : null}
      </div>
      <div
        role="separator"
        aria-label={separatorLabel}
        aria-orientation="vertical"
        aria-controls={`${previewId} ${contentId}`}
        aria-valuemin={MIN_RATIO}
        aria-valuemax={MAX_RATIO}
        aria-valuenow={Math.round(committedRatio)}
        aria-valuetext={`${Math.round(committedRatio)}% / ${Math.round(100 - committedRatio)}%`}
        tabIndex={open ? 0 : -1}
        onPointerDown={startDragging}
        onPointerMove={drag}
        onPointerUp={finishDragging}
        onPointerCancel={finishDragging}
        onDoubleClick={() => applyRatio(DEFAULT_RATIO, true)}
        onKeyDown={handleKeyDown}
        className={cn(
          "group relative hidden min-h-full touch-none select-none items-stretch justify-center outline-none",
          open && "lg:flex lg:cursor-col-resize",
          "focus-visible:rounded focus-visible:bg-primary/10 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
        )}
      >
        <span aria-hidden="true" className="w-0.5 bg-border transition-colors group-hover:bg-primary group-focus-visible:bg-primary" />
      </div>
      <div id={contentId} className="min-w-0">
        {children}
      </div>
    </div>
  );
}

function clampRatio(value: number) {
  return Math.min(MAX_RATIO, Math.max(MIN_RATIO, value));
}

function restoreDocumentInteraction(cursor: string, userSelect: string) {
  document.documentElement.style.cursor = cursor;
  document.documentElement.style.userSelect = userSelect;
}
