import { useCallback, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { apiClient, normalizeAPIError } from "@/api/client";
import { useCurrentUser } from "@/api/hooks";
import type { AdminUser } from "@/types/education";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { LibraryDialog } from "@/components/knowledge-base/LibraryDialog";

type Action = { label: string; path: string; method: "post" | "patch"; body: Record<string, string>; effect: string; dangerous?: boolean };
export function AdminUserDetailPage() {
  const { userId } = useParams();
  const navigate = useNavigate();
  const current = useCurrentUser();
  const cache = useQueryClient();
  const user = useQuery({ queryKey: ["admin", "user", userId], queryFn: async () => (await apiClient.get<AdminUser>(`/admin/users/${userId}`)).data });
  const [action, setAction] = useState<Action | null>(null);
  const [reason, setReason] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const key = useRef("");
  const close = useCallback(() => { if (!busyRef.current) { setAction(null); setError(""); } }, []);
  function open(next: Action) { key.current = crypto.randomUUID(); setReason(""); setConfirmation(""); setError(""); setAction(next); }
  async function submit() {
    if (!action || !reason.trim() || busyRef.current) return;
    busyRef.current = true; setBusy(true); setError("");
    try {
      const result = await apiClient.request({ url: `/admin/users/${userId}/${action.path}`, method: action.method, data: { ...action.body, reason: reason.trim(), ...(action.path === "closure" ? { confirm_username: confirmation } : {}) }, headers: { "Idempotency-Key": key.current } });
      if (action.path === "closure") {
        await cache.invalidateQueries({ queryKey: ["admin"] });
        navigate(`/admin/closures/${result.data.closure_id}`, { replace: true });
        return;
      }
      toast.success(action.path === "password-reset" ? "已发起密码重置邮件请求；用户完成重置后旧登录才会失效。" : "操作已完成，已记录原因。");
      await cache.invalidateQueries({ queryKey: ["admin"] });
      setAction(null);
    } catch (e) {
      const normalized = normalizeAPIError(e);
      const status = normalized.status;
      const detail = normalized.payload?.detail;
      if (detail && typeof detail === "object" && "code" in detail && detail.code === "account_has_shared_student_work") {
        setError("该账号可能参与其他教师的批改任务，请先处理相关任务后再销户。");
        return;
      }
      setError(status === 409 ? "当前账号状态不允许此操作，请刷新后检查；不能限制自己或最后一个管理员。" : status === 403 ? "管理权限已变化，请重新登录确认。" : "操作结果尚未确认。请重试核对；重试使用相同操作编号，避免重复执行。");
    } finally { busyRef.current = false; setBusy(false); }
  }
  if (user.isLoading) return <p role="status">正在加载账号…</p>;
  if (user.isError || !user.data) return <Card><p role="alert">无法读取账号，请检查账号是否存在或稍后重试。</p><Button onClick={() => void user.refetch()}>重试</Button><Link className="ml-4 text-primary" to="/admin/users">返回用户列表</Link></Card>;
  const u = user.data;
  const self = u.id === current.data?.id;
  const restricted = self || Boolean(u.closure);
  const access = (value: string, label: string, effect: string) => open({ label, path: "access", method: "patch", body: { access: value }, effect, dangerous: value !== "normal" });
  return <div className="space-y-5">
    <Link className="text-sm font-medium text-primary" to="/admin/users">← 返回用户管理</Link>
    <SectionHeader title={u.username} description={`${u.email || "未设置邮箱"} · ${u.role === "admin" ? "管理员" : u.role === "student" ? "历史学生账号" : "教师账号"}`} />
    {u.closure && <Card className="border-amber-300"><p role="status">该账号正在销户，无法恢复使用或重复执行管理操作。</p><Link className="mt-3 inline-block font-medium text-primary" to={`/admin/closures/${u.closure.id}`}>查看销户进度 →</Link></Card>}
    <Card><h2 className="text-base font-semibold">业务使用权限：{!u.is_active ? "禁止登录" : u.is_read_only ? "只读浏览" : "正常使用"}</h2><p className="mb-4 mt-2 text-sm text-muted-foreground">只读保留历史批改任务，可继续浏览；不会删除或撤销批改任务。</p><div className="flex flex-wrap gap-3">
      <Button variant="secondary" disabled={restricted || (u.is_active && !u.is_read_only)} onClick={() => access("normal", "恢复正常使用", "恢复新建、上传、批改和修改。此前撤销的登录令牌不会复活。")}>恢复正常使用</Button>
      <Button variant="secondary" disabled={restricted || u.is_read_only} onClick={() => access("read_only", "停用业务，保留只读", "用户仍可登录和浏览历史批改任务；禁止新建、上传、识别、批改及修改。现有任务不删除，登录也不撤销。")}>停用业务（只读）</Button>
      <Button variant="secondary" disabled={restricted || !u.is_active} onClick={() => access("login_blocked", "禁止登录", "立即退出全部登录，保留账号、用户名、邮箱和历史任务；这些身份暂不能再次注册。")}>禁止登录</Button>
    </div>{self && <p className="mt-3 text-sm text-muted-foreground">不能限制自己的管理权限。修改自己的密码请进入“管理员账号设置”。</p>}</Card>
    <Card><h2 className="font-semibold">登录与密码</h2><p className="mb-4 mt-2 text-sm text-muted-foreground">管理员无法查看原密码，也不会获得用户的重置凭证。</p><div className="flex flex-wrap gap-3">
      <Button variant="secondary" disabled={Boolean(u.closure)} onClick={() => open({ label: "撤销全部登录", path: "sessions/revoke", method: "post", body: {}, effect: "所有设备需要重新登录。历史批改任务和结果不受影响。", dangerous: true })}>撤销全部登录</Button>
      <Button variant="secondary" disabled={Boolean(u.closure) || !u.email || !u.is_active} onClick={() => open({ label: "发送密码重置邮件", path: "password-reset", method: "post", body: {}, effect: "链接发送到账号绑定邮箱。仅发送邮件不影响现有登录；用户成功重置密码后，全部旧登录失效。" })}>发送密码重置邮件</Button>
    </div></Card>
    <Card><h2 className="font-semibold">管理员授权</h2><p className="mb-4 mt-2 text-sm text-muted-foreground">只为需要承担平台管理职责的人授权。普通注册无需经过这里审批。</p><Button variant="secondary" disabled={restricted} onClick={() => open({ label: u.role === "admin" ? "撤销管理员授权" : "授予管理员权限", path: "role", method: "patch", body: { role: u.role === "admin" ? "teacher" : "admin" }, effect: "更改管理角色后，目标账号的全部登录立即失效，需要重新登录。不能撤销最后一个有效管理员。", dangerous: true })}>{u.role === "admin" ? "撤销管理员授权" : "授予管理员权限"}</Button></Card>
    <Card className="border-red-200"><h2 className="font-semibold text-danger">销户</h2><p className="mb-4 mt-2 text-sm text-muted-foreground">删除该账号及其业务数据。后台确认文件清理完成后才释放身份；清理失败会保留进度，不能把未完成当成功。</p><div className="flex flex-wrap gap-3">
      <Button variant="danger" disabled={restricted} onClick={() => open({ label: "正常销户", path: "closure", method: "post", body: { mode: "normal" }, effect: "立即禁止登录，随后删除账号、批改任务、教材和上传文件。完成后用户名与邮箱均释放，可以重新注册。确认后开始删除，无法通过恢复账号撤回。", dangerous: true })}>正常销户</Button>
      <Button variant="danger" disabled={restricted || !u.email} onClick={() => open({ label: "拉黑销户", path: "closure", method: "post", body: { mode: "blacklist" }, effect: "立即禁止登录，随后删除账号及业务数据。完成后用户名释放，原邮箱仍禁止注册；只匹配该邮箱，不封禁学校域名、IP 或设备。确认后开始删除，无法通过恢复账号撤回。", dangerous: true })}>拉黑销户</Button>
    </div></Card>
    {action && <LibraryDialog title={action.label} description={`目标账号：${u.username}`} closeLabel="关闭对话框" onClose={close} footer={<><Button variant="secondary" disabled={busy} onClick={close}>取消</Button><Button variant={action.dangerous ? "danger" : "primary"} disabled={busy || !reason.trim() || (action.path === "closure" && confirmation !== u.username)} onClick={() => void submit()}>{busy ? "正在处理…" : "确认操作"}</Button></>}>
      <p className="mb-4 text-sm leading-6">{action.effect}</p>{action.path === "closure" && <label className="mb-4 grid gap-2 text-sm">输入用户名 {u.username} 确认<input className="h-10 rounded-md border bg-background px-3" value={confirmation} disabled={busy} onChange={e => setConfirmation(e.target.value)} autoComplete="off" /></label>}<label className="grid gap-2 text-sm">操作原因（将记录到审计）<textarea maxLength={80} value={reason} disabled={busy} className="min-h-24 rounded-md border bg-background p-3" onChange={e => { setReason(e.target.value); if (error) { key.current = crypto.randomUUID(); setError(""); } }} /></label><p className="mt-2 text-xs text-muted-foreground">请勿填写密码、密钥或重置链接。</p>{error && <p role="alert" className="mt-3 text-sm text-danger">{error}</p>}
    </LibraryDialog>}
  </div>;
}
