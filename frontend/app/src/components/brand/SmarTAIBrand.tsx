import { cn } from "@/lib/cn";

export function SmarTAIWordmark({
  tone = "blue",
  alt = "SmarTAI",
  className,
}: {
  tone?: "blue" | "white";
  alt?: string;
  className?: string;
}) {
  return (
    <img
      alt={alt}
      className={cn("block h-auto select-none", className)}
      data-smartai-wordmark={tone}
      draggable={false}
      src={`/brand/smartai-wordmark-${tone}.svg`}
    />
  );
}

export function SmarTAIAppMark({
  finish = "flat-blue",
  alt = "",
  className,
}: {
  finish?: "flat-blue" | "crystal-blue" | "silver";
  alt?: string;
  className?: string;
}) {
  return (
    <img
      alt={alt}
      className={cn("block select-none object-contain", className)}
      data-smartai-app-mark={finish}
      draggable={false}
      src={`/brand/smartai-app-mark-${finish}.${finish === "flat-blue" ? "svg" : "png"}`}
    />
  );
}

export function SmarTAINavigationBrand() {
  return (
    <>
      <SmarTAIAppMark className="h-8 w-5 min-[390px]:hidden" />
      <SmarTAIWordmark
        alt=""
        className="hidden w-[104px] min-[390px]:block min-[420px]:w-[116px] sm:w-[132px] lg:w-[142px]"
      />
    </>
  );
}
