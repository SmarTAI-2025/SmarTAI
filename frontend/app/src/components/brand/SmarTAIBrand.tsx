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
