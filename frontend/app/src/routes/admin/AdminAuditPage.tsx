import { useAdminAudit } from "@/api/hooks/admin";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";

const actions: Record<string, string> = {
  first_admin_initialized: "初始化首位管理员",
  account_access_changed: "调整使用权限", account_role_changed: "调整管理员权限",
  user_deactivated: "禁止登录", user_reactivated: "恢复登录", sessions_revoked: "撤销全部登录",
  password_reset_assistance_requested: "请求发送密码重置邮件", own_password_changed: "修改本人密码",
  account_closure_requested: "提交销户", account_closure_completed: "销户完成",
  business_configuration_changed: "修改全局业务配置", user_storage_configuration_changed: "修改用户存储配额",
};
const fields: Record<string, string> = {
  role: "角色", is_active: "允许登录", is_read_only: "只读", mode: "销户方式", status: "处理状态",
  version: "配置版本", overrides: "覆盖设置", allowed_email_domains: "注册邮箱域名",
  email_verification_resend_seconds: "邮件冷却（秒）", email_verification_hourly_email_limit: "每邮箱每小时次数",
  email_verification_hourly_ip_limit: "每 IP 每小时次数", unfinished_source_quota_bytes: "批改原件存储（字节）",
  knowledge_storage_quota_bytes: "教材存储（字节）", auth_version: "登录版本", auth_invalid_before: "旧登录失效时间", updated_at: "更新时间",
};
function StateSummary({ state }: { state?: Record<string, unknown> | null }) {
  if (!state) return <span>未设置</span>;
  const entries = Object.entries(state);
  if (!entries.length) return <span>继承初始配置</span>;
  const labels: Record<string, string> = { admin: "管理员", teacher: "教师", normal: "正常销户", blacklist: "拉黑销户", pending: "处理中", completed: "已完成" };
  return <dl className="space-y-1">{entries.map(([key, value]) => <div key={key}><dt className="inline text-muted-foreground">{fields[key] ?? key}：</dt><dd className="inline break-words">{value && typeof value === "object" && !Array.isArray(value) ? <StateSummary state={value as Record<string, unknown>} /> : typeof value === "boolean" ? (value ? "是" : "否") : value === "" ? "空（禁止注册）" : value == null ? "继承" : labels[String(value)] ?? String(value)}</dd></div>)}</dl>;
}
export function AdminAuditPage() {
  const audit = useAdminAudit();
  return <div className="space-y-5"><SectionHeader title="操作审计" description="最近 50 条管理操作，包含修改人、原因和变更内容；提交销户与完成销户分别记录。" />
    <Button variant="secondary" disabled={audit.isFetching} onClick={() => void audit.refetch()}>{audit.isFetching ? "正在刷新…" : "刷新记录"}</Button>
    <Card className="overflow-hidden p-0">{audit.isLoading ? <div className="p-6 text-sm text-muted-foreground">加载中...</div> : audit.isError ? <div role="alert" className="p-6 text-sm text-danger">加载失败，请刷新记录重试。</div> : !audit.data?.length ? <div className="p-6 text-sm text-muted-foreground">暂无审计记录。</div> : <div className="overflow-x-auto"><table className="w-full min-w-[850px] text-left text-sm"><thead className="border-b text-xs text-muted-foreground"><tr>{["时间", "操作人", "操作与对象", "原因", "结果与变更"].map(label => <th key={label} className="px-4 py-3">{label}</th>)}</tr></thead><tbody>{audit.data.map(item => <tr key={item.id} className="border-b align-top last:border-0">
      <td className="whitespace-nowrap px-4 py-3">{new Date(item.created_at * 1000).toLocaleString()}</td>
      <td className="max-w-40 break-all px-4 py-3">{item.actor_name ?? (item.actor_id === "local-bootstrap" ? "本地维护终端" : "历史操作人")}<p className="mt-1 font-mono text-xs text-muted-foreground">{item.actor_id}</p></td>
      <td className="px-4 py-3">{actions[item.action] ?? item.action}{item.target_name && <p className="mt-1 font-medium">{item.target_name}</p>}<p className="mt-1 max-w-44 break-all font-mono text-xs text-muted-foreground">{item.target_user_id ?? "全局配置"}</p></td>
      <td className="max-w-48 break-words px-4 py-3">{item.reason}{item.note && <p className="mt-1 text-xs text-muted-foreground">{item.note}</p>}</td>
      <td className="px-4 py-3">{item.result === "success" ? "已记录" : item.result}{(item.before_state || item.after_state) && <details className="mt-2 min-w-48"><summary className="cursor-pointer text-primary">查看变更</summary><div className="mt-2 space-y-3 rounded-md bg-muted/40 p-3 text-xs"><div><p className="mb-1 font-semibold">修改前</p><StateSummary state={item.before_state} /></div><div><p className="mb-1 font-semibold">修改后</p><StateSummary state={item.after_state} /></div></div></details>}</td>
    </tr>)}</tbody></table></div>}</Card>
  </div>;
}
