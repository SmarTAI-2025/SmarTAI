import { cn } from "@/lib/cn";

export type SmarTAIMascotVariant = "idle" | "thinking" | "grading";
export type SmarTAIMascotSize = "xs" | "sm" | "md" | "lg";

const STATIC_SRC = "/brand/smartai-mascot.svg";

const ANIMATED_SRC: Record<Exclude<SmarTAIMascotVariant, "idle">, string> = {
  thinking: "/brand/smartai-loading-blink-thinking.svg",
  grading: "/brand/smartai-loading-rapid-grading.svg",
};

const SIZE_CLASS: Record<SmarTAIMascotSize, string> = {
  xs: "h-9 w-11",
  sm: "h-12 w-16",
  md: "h-20 w-28",
  lg: "h-24 w-36",
};

export function SmarTAIMascot({
  variant = "idle",
  size = "md",
  className,
}: {
  variant?: SmarTAIMascotVariant;
  size?: SmarTAIMascotSize;
  className?: string;
}) {
  const animatedSrc = variant === "idle" ? null : ANIMATED_SRC[variant];

  return (
    <span
      aria-hidden="true"
      data-smartai-mascot={variant}
      className={cn("inline-flex shrink-0 items-center justify-center", SIZE_CLASS[size], className)}
    >
      {animatedSrc ? (
        <img
          alt=""
          draggable={false}
          src={animatedSrc}
          className="h-full w-full select-none object-contain motion-reduce:hidden"
        />
      ) : null}
      <img
        alt=""
        draggable={false}
        src={STATIC_SRC}
        className={cn(
          "h-full w-full select-none object-contain",
          animatedSrc && "hidden motion-reduce:block",
        )}
      />
    </span>
  );
}
