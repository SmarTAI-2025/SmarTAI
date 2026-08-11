import { cn } from "@/lib/cn";

export function SmarTAIWordmark({
  alt = "SmarTAI",
  className,
}: {
  alt?: string;
  className?: string;
}) {
  return (
    <img
      alt={alt}
      className={cn("block h-auto select-none", className)}
      data-smartai-wordmark="blue"
      draggable={false}
      src="/brand/smartai-wordmark-blue.svg"
    />
  );
}

export function SmarTAIAppMark({
  alt = "",
  className,
}: {
  alt?: string;
  className?: string;
}) {
  return (
    <img
      alt={alt}
      className={cn("block select-none object-contain", className)}
      data-smartai-app-mark="flat-blue"
      draggable={false}
      src="/brand/smartai-app-mark-flat-blue.svg"
    />
  );
}
