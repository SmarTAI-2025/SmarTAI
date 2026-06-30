import {
  BrainCircuit,
  ClipboardList,
  History,
  LayoutDashboard,
  LogOut,
  Menu,
  PlusCircle,
  Settings,
  X,
} from "lucide-react";
import { useState } from "react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { useLogout } from "@/api/hooks";
import { Button } from "@/components/ui/Button";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";

const navItems = [
  { to: "/", labelKey: "dashboard", icon: LayoutDashboard },
  { to: "/tasks/new", labelKey: "newTask", icon: PlusCircle },
  { to: "/history", labelKey: "history", icon: History },
  { to: "/experts", labelKey: "experts", icon: BrainCircuit },
  { to: "/settings", labelKey: "settings", icon: Settings },
] as const;

export function AppShell() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const { t } = useI18n();
  const logout = useLogout();
  const navigate = useNavigate();

  async function handleLogout() {
    try {
      await logout.mutateAsync();
    } finally {
      navigate("/login", { replace: true });
    }
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-64 border-r bg-card md:block">
        <ShellNav />
      </aside>
      <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b bg-card/95 px-4 backdrop-blur md:ml-64">
        <button
          type="button"
          className="inline-flex h-9 w-9 items-center justify-center rounded-md border md:hidden"
          onClick={() => setMobileOpen(true)}
          aria-label="Open navigation"
        >
          <Menu className="h-5 w-5" />
        </button>
        <div className="flex items-center gap-2 font-semibold">
          <ClipboardList className="h-5 w-5 text-primary" />
          {t("appName")}
        </div>
        <div className="ml-auto">
          <Button
            variant="ghost"
            className="h-8"
            onClick={() => void handleLogout()}
            disabled={logout.isPending}
          >
            <LogOut className="h-4 w-4" />
            {t("logout")}
          </Button>
        </div>
      </header>
      {mobileOpen ? (
        <div className="fixed inset-0 z-50 md:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-black/40"
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
          />
          <aside className="relative h-full w-72 border-r bg-card">
            <button
              type="button"
              className="absolute right-3 top-3 inline-flex h-8 w-8 items-center justify-center rounded-md border"
              onClick={() => setMobileOpen(false)}
              aria-label="Close navigation"
            >
              <X className="h-4 w-4" />
            </button>
            <ShellNav onNavigate={() => setMobileOpen(false)} />
          </aside>
        </div>
      ) : null}
      <main className="px-4 py-5 md:ml-64 md:px-6">
        <Outlet />
      </main>
    </div>
  );
}

function ShellNav({ onNavigate }: { onNavigate?: () => void }) {
  const { t } = useI18n();
  return (
    <div className="flex h-full flex-col">
      <Link to="/" className="flex h-16 items-center gap-2 px-5 text-lg font-semibold">
        <ClipboardList className="h-6 w-6 text-primary" />
        SmarTAI
      </Link>
      <nav className="grid gap-1 px-3">
        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              onClick={onNavigate}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition",
                  isActive
                    ? "bg-primary text-primary-foreground"
                    : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )
              }
            >
              <Icon className="h-4 w-4" />
              {t(item.labelKey)}
            </NavLink>
          );
        })}
      </nav>
      <div className="mt-auto border-t p-4 text-xs leading-5 text-muted-foreground">
        当前仅展示教师端主流程；LMS、课程作业和学生端入口保持隐藏。
      </div>
    </div>
  );
}
