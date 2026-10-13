import { NavLink, useLocation } from "react-router-dom";
import { PRIMARY_NAVIGATION } from "@/components/layout/navigation";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { useFrontierWorkspace } from "@/lib/frontierContext";
import { taskWorkspacePath } from "@/lib/frontierSession";

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
  const { t } = useI18n();
  const location = useLocation();
  const frontier = useFrontierWorkspace();
  if (frontier) return <nav aria-label={t("primaryNavigation")} className={className}><NavLink to={location.pathname === "/frontier/live" ? `${location.pathname}${location.search}` : taskWorkspacePath(location.pathname.split("/")[2])} onClick={onNavigate} className={cn("rounded-lg px-4 font-semibold text-primary", mobile ? "py-3" : "py-2.5")}>Demo</NavLink></nav>;

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
