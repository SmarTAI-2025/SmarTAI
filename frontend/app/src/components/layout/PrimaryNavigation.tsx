import { NavLink, useLocation } from "react-router-dom";
import { PRIMARY_NAVIGATION } from "@/components/layout/navigation";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { useFrontierWorkspace } from "@/lib/frontierContext";
import { isFrontierTaskPath, taskWorkspacePath } from "@/lib/frontierSession";

interface PrimaryNavigationProps {
  className?: string;
  mobile?: boolean;
  onNavigate?: () => void;
}

/** Shared navigation renderer for the desktop header and mobile drawer. */
export function PrimaryNavigation({
  className,
  mobile = false,
  onNavigate,
}: PrimaryNavigationProps) {
  const { t, locale } = useI18n();
  const location = useLocation();
  const frontier = useFrontierWorkspace();
  if (frontier) {
    const requestedId = location.pathname === "/frontier/live"
      ? new URLSearchParams(location.search).get("taskId") : location.pathname.split("/")[2];
    const taskId = requestedId && isFrontierTaskPath(`/tasks/${requestedId}`) ? requestedId : null;
    const brief = taskId ? taskWorkspacePath(taskId) : "/frontier/live";
    const detailed = taskId ? `/tasks/${encodeURIComponent(taskId)}/edit` : "/frontier/live?workflow=full";
    return <nav aria-label={t("primaryNavigation")} className={className}>
      {[{ href: brief, label: locale === "zh-CN" ? "简明演示" : "Quick Demo" }, { href: detailed, label: locale === "zh-CN" ? "详细流程" : "Full Workflow" }].map(item => <NavLink key={item.href} to={item.href} onClick={onNavigate} className={cn("rounded-lg px-4 text-[13px] font-semibold text-primary", mobile ? "py-3" : "py-2.5")}>{item.label}</NavLink>)}
    </nav>;
  }

  return (
    <nav aria-label={t("primaryNavigation")} className={className}>
      {PRIMARY_NAVIGATION.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/dashboard"}
          onClick={onNavigate}
          aria-current={item.to === "/tasks/new" && location.pathname.startsWith("/tasks/") ? "page" : undefined}
          className={({ isActive }) =>
            cn(
              "rounded-lg text-[13px] font-medium leading-4 text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card",
              mobile ? "px-4 py-3" : "px-4 py-2.5",
              (isActive || (item.to === "/tasks/new" && location.pathname.startsWith("/tasks/")))
                && "bg-primary/[0.08] font-semibold text-primary",
            )
          }
        >
          {t(item.labelKey)}
        </NavLink>
      ))}
    </nav>
  );
}
