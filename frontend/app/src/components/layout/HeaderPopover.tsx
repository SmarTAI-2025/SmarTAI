import {
  useEffect,
  useId,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { cn } from "@/lib/cn";

interface HeaderPopoverProps {
  ariaLabel: string;
  renderTrigger: (open: boolean) => ReactNode;
  children: (close: () => void) => ReactNode;
  triggerClassName?: string;
  panelClassName?: string;
}

/** Accessible, dependency-free popover used by the model and account menus. */
export function HeaderPopover({
  ariaLabel,
  renderTrigger,
  children,
  triggerClassName,
  panelClassName,
}: HeaderPopoverProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) {
      return;
    }

    window.requestAnimationFrame(() => {
      panelRef.current?.querySelector<HTMLElement>("[data-popover-focus]")?.focus();
    });

    function handlePointerDown(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  function close() {
    setOpen(false);
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls={open ? panelId : undefined}
        className={cn(
          "inline-flex h-9 items-center justify-center rounded-lg text-[13px] font-medium leading-4 text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card",
          open && "bg-muted text-foreground",
          triggerClassName,
        )}
        onClick={() => setOpen((current) => !current)}
      >
        {renderTrigger(open)}
      </button>
      {open ? (
        <div
          ref={panelRef}
          id={panelId}
          role="dialog"
          aria-label={ariaLabel}
          className={cn(
            "absolute right-0 top-[calc(100%+10px)] z-50 w-72 rounded-[10px] border bg-card p-2 text-card-foreground shadow-[0_18px_50px_rgba(15,23,42,0.14)]",
            panelClassName,
          )}
        >
          {children(close)}
        </div>
      ) : null}
    </div>
  );
}
