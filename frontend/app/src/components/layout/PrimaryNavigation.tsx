import { NavLink, useLocation } from "react-router-dom";
import { PRIMARY_NAVIGATION } from "@/components/layout/navigation";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";

interface PrimaryNavigationProps {
  className?: string;
  mobile?: boolean;
  onNavigate?: () => void;
  demoLivePath?: string;
  demoTaskPath?: string;
}

/** Shared navigation renderer for the desktop header and mobile drawer. */
export function PrimaryNavigation({
  className,
  mobile = false,
  onNavigate,
  demoLivePath,
  demoTaskPath,
}: PrimaryNavigationProps) {
  const { locale, t } = useI18n();
  const location = useLocation();
  const items = demoLivePath
    ? [
        { to: "/frontier", label: locale === "zh-CN" ? "产品介绍" : "Product overview" },
        { to: demoLivePath, label: "Live Demo" },
        ...(demoTaskPath ? [{ to: demoTaskPath, label: locale === "zh-CN" ? "当前任务" : "Current task" }] : []),
      ]
    : PRIMARY_NAVIGATION.map((item) => ({ to: item.to, label: t(item.labelKey) }));

  return (
    <nav aria-label={t("primaryNavigation")} className={className}>
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === "/" || item.to === "/frontier"}
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
          {item.label}
        </NavLink>
      ))}
    </nav>
  );
}
