import { Link } from "react-router-dom";
import { EmptyState } from "@/components/ui/EmptyState";
import { useI18n } from "@/i18n/I18nProvider";

export function NotFoundPage() {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";

  return (
    <EmptyState
      title={zh ? "页面不存在" : "Page not found"}
      description={zh ? "请返回工作台继续操作。" : "Return to the workspace to continue."}
      action={
        <Link
          to="/"
          className="inline-flex h-9 items-center justify-center rounded-md border bg-card px-3 text-sm font-medium text-foreground transition hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        >
          {zh ? "返回工作台" : "Back to workspace"}
        </Link>
      }
    />
  );
}
