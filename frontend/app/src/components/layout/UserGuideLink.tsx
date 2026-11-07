import type { MouseEvent } from "react";
import { useDraftLeave } from "@/hooks/useDraftLeave";
import { useI18n } from "@/i18n/I18nProvider";

export function UserGuideLink({ className, onNavigate }: { className?: string; onNavigate?: () => void }) {
  const { locale, t } = useI18n();
  const leave = useDraftLeave();
  const href = `/guide/?lang=${locale === "zh-CN" ? "zh" : "en"}`;

  function openGuide(event: MouseEvent<HTMLAnchorElement>) {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    onNavigate?.();
    if (leave.controllers.some(controller => controller.dirty || controller.busy)) {
      event.preventDefault();
      leave.request(() => window.location.assign(href));
    }
  }

  return <a className={className} href={href} onClick={openGuide}>{t("userGuide")}</a>;
}
