import { useAdminAudit } from "@/api/hooks/admin";
import { Card, SectionHeader } from "@/components/ui/Card";

export function AdminAuditPage() {
  const audit = useAdminAudit();
  return <div className="space-y-5"><SectionHeader title="操作审计" description="记录管理员对账号和会话执行的操作、原因与结果。" /><Card className="p-0">{audit.isLoading ? <div className="p-6 text-sm text-muted-foreground">加载中...</div> : audit.isError ? <div className="p-6 text-sm text-danger">加载失败，请稍后重试。</div> : !audit.data?.length ? <div className="p-6 text-sm text-muted-foreground">暂无审计记录。</div> : <table className="w-full text-sm"><thead className="border-b text-left text-xs text-muted-foreground"><tr><th className="px-4 py-2">时间</th><th className="px-4 py-2">动作</th><th className="px-4 py-2">目标</th><th className="px-4 py-2">原因</th><th className="px-4 py-2">备注</th></tr></thead><tbody>{audit.data.map((item) => <tr key={item.id} className="border-b last:border-0"><td className="px-4 py-2">{new Date(item.created_at * 1000).toLocaleString()}</td><td className="px-4 py-2">{item.action}</td><td className="px-4 py-2 font-mono">{item.target_user_id ?? "—"}</td><td className="px-4 py-2">{item.reason}</td><td className="px-4 py-2 text-muted-foreground">{item.note || "—"}</td></tr>)}</tbody></table>}</Card></div>;
}
