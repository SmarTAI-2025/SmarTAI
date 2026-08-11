import { cn } from "@/lib/cn";

export type SmarTAIWordmarkTone = "blue" | "white";
export type SmarTAIAppMarkFinish = "flat-blue" | "crystal-blue" | "silver";

const WORDMARK_SRC: Record<SmarTAIWordmarkTone, string> = {
  blue: "/brand/smartai-wordmark-blue.svg",
  white: "/brand/smartai-wordmark-white.svg",
};

const APP_MARK_SRC: Record<SmarTAIAppMarkFinish, string> = {
  "flat-blue": "/brand/smartai-app-mark-flat-blue.svg",
  "crystal-blue": "/brand/smartai-app-mark-crystal-blue.png",
  silver: "/brand/smartai-app-mark-silver.png",
};

export function SmarTAIWordmark({
  tone = "blue",
  alt = "SmarTAI",
  className,
}: {
  tone?: SmarTAIWordmarkTone;
  alt?: string;
  className?: string;
}) {
  return (
    <img
      alt={alt}
      className={cn("block h-auto select-none", className)}
      data-smartai-wordmark={tone}
      draggable={false}
      src={WORDMARK_SRC[tone]}
    />
  );
}

export function SmarTAIAppMark({
  finish = "flat-blue",
  alt = "",
  className,
}: {
  finish?: SmarTAIAppMarkFinish;
  alt?: string;
  className?: string;
}) {
  return (
    <img
      alt={alt}
      className={cn("block select-none object-contain", className)}
      data-smartai-app-mark={finish}
      draggable={false}
      src={APP_MARK_SRC[finish]}
    />
  );
}
