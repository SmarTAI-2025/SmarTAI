import { useI18n } from "@/i18n/I18nProvider";
import { Activity, Wrench, TrendingUp, BarChart3, Settings, FileClock, LogOut, Shield, Users } from "lucide-react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { useCurrentUser } from "@/api/hooks";
import { useState, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiClient, clearAuthToken } from "@/api/client";
import { toast } from "sonner";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";

const links = [
  { to: "/admin", label: "概览", icon: BarChart3, end: true },
  { to: "/admin/users", label: "用户管理", icon: Users },
  { to: "/admin/monitoring", label: "运行监控", icon: Activity },
  { to: "/admin/analytics", label: "运营统计", icon: TrendingUp },
  { to: "/admin/maintenance", label: "系统维护", icon: Wrench },
  { to: "/admin/account", label: "管理员账号设置", icon: Settings },
  { to: "/admin/audit", label: "操作审计", icon: FileClock },
];

export function AdminShell() {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const user = useCurrentUser();
  const [signingOut, setSigningOut] = useState(false);
  const signOutActive = useRef(false);
  const cache = useQueryClient();
  const navigate = useNavigate();

  async function signOut() {
    if (signOutActive.current) return;
    signOutActive.current = true; setSigningOut(true);
    try {
      await apiClient.post("/auth/logout"); clearAuthToken(); cache.clear();
      navigate("/login", { replace: true });
    } catch { toast.error("退出尚未完成，请检查网络后重试。"); }
    finally { signOutActive.current = false; setSigningOut(false); }
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex h-16 max-w-7xl items-center gap-6 px-5 sm:px-8">
          <Link to="/admin" className="flex items-center gap-2 font-semibold"><Shield size={20} className="text-primary" />SmarTAI 管理端</Link>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="hidden text-muted-foreground sm:inline">{user.data?.username ?? "管理员"}</span>
            <Button variant="ghost" className="h-8" onClick={() => void signOut()} disabled={signingOut}><LogOut size={16} />{zh ? "退出" : "Sign out"}</Button>
          </div>
        </div>
      </header>
      <div className="mx-auto flex w-full max-w-[1500px] flex-col lg:flex-row">
        <nav aria-label={zh ? "管理员导航" : "Administration navigation"} className="grid grid-cols-2 gap-1 border-b bg-card p-3 sm:grid-cols-4 lg:sticky lg:top-0 lg:h-[calc(100vh-4rem)] lg:w-56 lg:shrink-0 lg:grid-cols-1 lg:content-start lg:border-b-0 lg:border-r lg:py-6">
          {links.map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => cn("flex items-center gap-2 rounded-lg px-3 py-3 text-sm text-muted-foreground hover:bg-muted", isActive && "bg-primary/[0.08] font-semibold text-primary")}><Icon size={17} className="shrink-0" />{label}</NavLink>)}
        </nav>
        <main className="min-w-0 flex-1 px-5 py-7 sm:px-8"><Outlet /></main>
      </div>
    </div>
  );
}
