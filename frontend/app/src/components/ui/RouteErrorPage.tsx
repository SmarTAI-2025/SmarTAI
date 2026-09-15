import { useRouteError } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { useI18n } from "@/i18n/I18nProvider";
import { isAssetLoadError } from "@/lib/assetLoadRecovery";

export function RouteErrorPage() {
  const error = useRouteError();
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const assetFailure = isAssetLoadError(error);

  return (
    <main className="mx-auto flex min-h-[60vh] max-w-xl flex-col justify-center gap-4 p-6" role="alert">
      <h1 className="text-xl font-bold">
        {assetFailure
          ? zh ? "页面资源未能加载" : "Page resources could not load"
          : zh ? "页面暂时无法显示" : "This page could not be displayed"}
      </h1>
      <p className="text-sm leading-6 text-muted-foreground">
        {assetFailure
          ? zh
            ? "页面可能刚刚更新，或网络连接中断。请重新加载当前页面，任务地址会保留。"
            : "The app may have been updated, or the connection was interrupted. Reload this page to continue at the same task address."
          : zh
            ? "请重新加载当前页面。如果仍然出现此问题，请向管理员反馈当前页面地址。"
            : "Reload this page. If the problem continues, share the current page address with the administrator."}
      </p>
      <Button className="self-start" onClick={() => window.location.reload()}>
        {zh ? "重新加载当前页面" : "Reload current page"}
      </Button>
    </main>
  );
}
