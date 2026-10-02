import { useState } from "react";
import { Link } from "react-router-dom";
import { useAdminUsers } from "@/api/hooks/admin";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";

export function AdminUsersPage() {
  const [search, setSearch] = useState("");
  const [role, setRole] = useState("");
  const [page, setPage] = useState(1);
  const users = useAdminUsers({ search: search || undefined, role: role || undefined, page, page_size: 25 });
  const result = users.data;
  const items = Array.isArray(result) ? result : result?.items ?? [];
  return <div className="space-y-5">
    <SectionHeader title="用户管理" description="处理账号权限与违规情况。普通用户继续通过现有邮箱流程注册，无需管理员逐人审批。" />
    <Card className="flex flex-wrap gap-4">
      <label className="grid grow gap-1 text-sm"><span>搜索用户</span><input className="h-10 rounded-md border bg-background px-3" placeholder="用户名或邮箱" value={search} onChange={e => { setSearch(e.target.value); setPage(1); }} /></label>
      <label className="grid gap-1 text-sm"><span>角色</span><select className="h-10 rounded-md border bg-background px-3" value={role} onChange={e => { setRole(e.target.value); setPage(1); }}><option value="">全部</option><option value="teacher">教师</option><option value="admin">管理员</option><option value="student">历史学生账号</option></select></label>
    </Card>
    <Card className="overflow-x-auto p-0">
      {users.isLoading ? <p role="status" className="p-6">加载中...</p> : users.isError ? <div role="alert" className="p-6">加载失败，请重试。 <Button onClick={() => void users.refetch()}>重试</Button></div> : !items.length ? <EmptyState title="暂无用户" description="没有符合筛选条件的账号。" /> :
      <table className="w-full min-w-[580px] text-left text-sm"><thead className="border-b bg-muted/40 text-muted-foreground"><tr>{["用户", "邮箱", "角色", "访问权限", "操作"].map(x => <th key={x} className="px-4 py-3">{x}</th>)}</tr></thead><tbody>
        {items.map(u => <tr key={u.id} className="border-b last:border-0"><td className="px-4 py-4 font-medium"><Link className="text-primary hover:underline" to={`/admin/users/${u.id}`}>{u.username}</Link></td><td className="px-4 py-4">{u.email || "未设置"}</td><td className="px-4 py-4">{u.role === "admin" ? "管理员" : u.role === "teacher" ? "教师" : "历史学生"}</td><td className="px-4 py-4">{!u.is_active ? "禁止登录" : u.is_read_only ? "只读浏览" : "正常使用"}</td><td className="px-4 py-4"><Link className="inline-flex rounded-md border px-3 py-2 font-medium text-primary hover:bg-muted" to={`/admin/users/${u.id}`}>管理账号</Link></td></tr>)}
      </tbody></table>}
    </Card>
    {result && !Array.isArray(result) && result.total > 0 && <div className="flex items-center justify-between gap-3 text-sm"><span>共 {result.total} 个账号 · 第 {page} 页</span><div className="flex gap-2"><Button variant="secondary" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>上一页</Button><Button variant="secondary" disabled={!result.has_next} onClick={() => setPage(p => p + 1)}>下一页</Button></div></div>}
  </div>;
}
