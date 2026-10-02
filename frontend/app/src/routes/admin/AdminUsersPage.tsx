import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { adminRequestPasswordReset, adminRevokeSessions, type AdminUsersPage } from "@/api/admin";
import { useAdminUsers, useAdminSetActive } from "@/api/hooks/admin";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Admin user management: list every account with role + active filters, and
 * activate/deactivate inline. Deactivation is preferred over deletion (FK
 * references remain valid); the backend refuses to deactivate a teacher who
 * still owns a course, surfaced here as a toast.
 */
export function AdminUsersPage() {
  const [role, setRole] = useState<string>("");
  const [activeOnly, setActiveOnly] = useState<boolean>(false);
  const [search, setSearch] = useState<string>("");
  const [page, setPage] = useState(1);
  const users = useAdminUsers({
    role: role || undefined,
    is_active: activeOnly ? true : undefined,
    search: search || undefined,
    page,
    page_size: 25,
  });
  const setActive = useAdminSetActive();
  const revokeSessions = useMutation({ mutationFn: (userId: string) => adminRevokeSessions(userId, "device_or_session_support") });
  const resetPassword = useMutation({ mutationFn: (userId: string) => adminRequestPasswordReset(userId, "account_support") });
  const rawData = users.data;
  const pagedData = rawData && !Array.isArray(rawData) ? rawData as AdminUsersPage : null;
  const items = rawData ? (Array.isArray(rawData) ? rawData : rawData.items) : [];

  function askReason(action: string): string | null {
    const reason = window.prompt(`${action}原因（会写入操作审计）：`, "account_support");
    return reason?.trim() || null;
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader
        title="用户管理"
        description="查看全部账号、按角色与状态筛选，并启停账户。停用优先于删除；仍拥有课程的教师无法停用。"
      />
      <Card className="flex flex-wrap items-end gap-4">
        <label className="grid gap-1 text-sm">
          <span className="font-medium">角色</span>
          <select
            className="h-9 rounded-md border bg-background px-2 text-sm"
            value={role}
            onChange={(e) => setRole(e.target.value)}
          >
            <option value="">全部</option>
            <option value="teacher">teacher</option>
            <option value="student">student</option>
            <option value="admin">admin</option>
          </select>
        </label>
          <label className="grid min-w-56 gap-1 text-sm">
            <span className="font-medium">搜索</span>
            <input
              className="h-9 rounded-md border bg-background px-2 text-sm"
              value={search}
              onChange={(e) => { setSearch(e.target.value); setPage(1); }}
              placeholder="用户名或邮箱"
            />
          </label>
          <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={activeOnly}
            onChange={(e) => setActiveOnly(e.target.checked)}
          />
          仅显示启用
        </label>
      </Card>

      <Card className="p-0">
        {users.isLoading ? (
          <div className="p-6 text-sm text-muted-foreground">加载中...</div>
        ) : users.isError ? (
          <div className="p-6 text-sm text-danger">加载失败，请稍后重试。</div>
        ) : items.length === 0 ? (
          <EmptyState title="暂无用户" description="没有符合筛选条件的账号。" />
        ) : (
          <table className="w-full text-sm">
            <thead className="border-b text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-2">用户名</th>
                <th className="px-4 py-2">邮箱</th>
                <th className="px-4 py-2">角色</th>
                <th className="px-4 py-2">状态</th>
                <th className="px-4 py-2 text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((u) => (
                <tr key={u.id} className="border-b last:border-0">
                  <td className="px-4 py-2 font-medium">{u.username}</td>
                  <td className="px-4 py-2 text-muted-foreground">{u.email || "—"}</td>
                  <td className="px-4 py-2">{u.role}</td>
                  <td className="px-4 py-2">
                    {u.is_active ? (
                      <span className="text-emerald-600">启用</span>
                    ) : (
                      <span className="text-muted-foreground">停用</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Button
                      variant={u.is_active ? "secondary" : "primary"}
                      className="h-8"
                      disabled={setActive.isPending}
                      onClick={() => {
                        const reason = askReason(u.is_active ? "停用账号" : "恢复账号");
                        if (reason) setActive.mutate({ userId: u.id, isActive: !u.is_active, reason });
                      }}
                    >
                      {u.is_active ? "停用" : "启用"}
                    </Button>
                    <Button
                      variant="ghost"
                      className="ml-2 h-8"
                      disabled={revokeSessions.isPending}
                      onClick={() => { if (askReason("撤销全部会话")) revokeSessions.mutate(u.id); }}
                    >
                      撤销会话
                    </Button>
                    <Button
                      variant="ghost"
                      className="ml-2 h-8"
                      disabled={resetPassword.isPending || !u.email}
                      onClick={() => { if (askReason("发送重置邮件")) resetPassword.mutate(u.id); }}
                    >
                      重置密码
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      {pagedData && pagedData.total > 0 ? (
        <div className="flex items-center justify-between text-sm text-muted-foreground">
          <span>共 {pagedData.total} 个账号</span>
          <Button variant="secondary" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>上一页</Button>
          <span>第 {page} 页</span>
          <Button variant="secondary" disabled={!pagedData.has_next} onClick={() => setPage((value) => value + 1)}>下一页</Button>
        </div>
      ) : null}
    </div>
  );
}
