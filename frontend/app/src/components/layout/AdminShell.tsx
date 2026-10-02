import { BarChart3, ClipboardList, FileClock, LogOut, Shield, Users } from "lucide-react";
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { useCurrentUser, useLogout } from "@/api/hooks";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";

const links = [
  { to: "/admin", label: "概览", icon: BarChart3, end: true },
  { to: "/admin/users", label: "用户与权限", icon: Users },
  { to: "/admin/invites", label: "邀请管理", icon: ClipboardList },
  { to: "/admin/audit", label: "操作审计", icon: FileClock },
];

export function AdminShell() {
  const user = useCurrentUser();
  const logout = useLogout();
  const navigate = useNavigate();

  async function signOut() {
    try { await logout.mutateAsync(); } finally { navigate("/login", { replace: true }); }
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b bg-card">
        <div className="mx-auto flex h-16 max-w-7xl items-center gap-6 px-5 sm:px-8">
          <Link to="/admin" className="flex items-center gap-2 font-semibold"><Shield size={20} className="text-primary" />SmarTAI 管理端</Link>
          <nav className="hidden items-center gap-1 md:flex">
            {links.map(({ to, label, icon: Icon, end }) => (
              <NavLink key={to} to={to} end={end} className={({ isActive }) => cn("flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-muted-foreground hover:bg-muted hover:text-foreground", isActive && "bg-primary/[0.08] font-semibold text-primary")}>
                <Icon size={16} />{label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3 text-sm">
            <span className="hidden text-muted-foreground sm:inline">{user.data?.username ?? "管理员"}</span>
            <Button variant="ghost" className="h-8" onClick={() => void signOut()} disabled={logout.isPending}><LogOut size={16} />退出</Button>
          </div>
        </div>
      </header>
      <nav className="flex gap-1 overflow-x-auto border-b bg-card px-5 py-2 md:hidden">
        {links.map(({ to, label, end }) => <NavLink key={to} to={to} end={end} className={({ isActive }) => cn("whitespace-nowrap rounded px-3 py-1.5 text-sm text-muted-foreground", isActive && "bg-primary/[0.08] font-semibold text-primary")}>{label}</NavLink>)}
      </nav>
      <main className="mx-auto w-full max-w-7xl px-5 py-8 sm:px-8"><Outlet /></main>
    </div>
  );
}
