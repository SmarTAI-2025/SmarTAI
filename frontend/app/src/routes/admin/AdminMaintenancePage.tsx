import { useDraftProtection } from "@/hooks/useDraftProtection";
import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { backendUrl, getAuthToken } from "@/api/client";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { LibraryDialog } from "@/components/knowledge-base/LibraryDialog";

type Preview = { available: boolean; execution_available: boolean; environment: string; reason?: string; fingerprint?: string; confirmation?: string; target?: { database: string; local_roots: string[]; object_scope: string | null }; tables?: Record<string, number>; storage?: { files: number; versions_and_markers: number; multipart_uploads: number; bytes: number } };
type Status = { fingerprint: string; status: string; phase?: string; error_code?: string; files_remaining?: number; rows_deleted?: number };
async function maintenance<T>(path: string, body?: unknown): Promise<T> {
  const token = getAuthToken();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 30000);
  try {
  const response = await fetch(`${backendUrl}/admin/maintenance/${path}`, { signal: controller.signal, method: body ? "POST" : "GET", credentials: "include", headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(body ? { "Content-Type": "application/json" } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail?.code || `HTTP_${response.status}`);
  return data;
  } finally { clearTimeout(timeout); }
}
const errors: Record<string, string> = {
  reset_maintenance_password_invalid: "维护密码错误。请核对后重试。连续 5 次失败将暂停验证 15 分钟。",
  reset_maintenance_password_rate_limited: "维护密码连续验证失败，请等待 15 分钟后重试。",
  reset_preview_stale: "数据自预览后发生变化。尚未清理，请刷新预览并重新确认。",
  reset_services_or_executor_running: "普通服务、后台任务或其他清理进程仍在运行。请停止它们后重试。",
  reset_confirmation_mismatch: "确认短语或停止服务确认不正确，未开始清理。",
  reset_offline_service_required: "请停止普通服务，改用专用维护服务进行清理。",
  HTTP_401: "管理员登录已过期，请重新登录。已有维护记录会保留。",
  HTTP_403: "没有管理员权限，或页面来源不匹配。",
};
export function AdminMaintenancePage({ standalone = false }: { standalone?: boolean }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [dialog, setDialog] = useState(false), [password, setPassword] = useState(""), [confirmation, setConfirmation] = useState(""), [stopped, setStopped] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const protection = useDraftProtection({ scope: "admin-maintenance-confirm", value: { password, confirmation, stopped }, secret: true, enabled: dialog, busy, onRestore: () => { setPassword(""); setConfirmation(""); setStopped(false); } });
  const active = useRef(false), knownOperation = useRef(false);
  const preview = useQuery({ queryKey: ["admin", "reset-preview"], queryFn: () => maintenance<Preview>("preview"), retry: false, enabled: !status, refetchOnWindowFocus: false });
  const p = preview.data;
  useEffect(() => {
    if (!standalone) return;
    let cancelled = false; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { const value = await maintenance<Status>("status"); if (!cancelled) { knownOperation.current = true; setStatus(value); setError(""); if (value.status === "running") timer = setTimeout(() => void poll(), 1500); } }
      catch { if (!cancelled && knownOperation.current) { setError("暂时无法读取执行状态。清理可能仍在进行；请刷新此页面恢复查询，不要启动其他服务。"); timer = setTimeout(() => void poll(), 3000); } }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [standalone, busy]);
  const fingerprint = status?.fingerprint || p?.fingerprint;
  const phrase = status ? `ERASE ALL DISPOSABLE BUSINESS DATA ${status.fingerprint.slice(0, 12)}` : p?.confirmation;
  const canConfirm = !!password && confirmation === phrase && stopped && !busy;
  async function execute() {
    if (active.current || !canConfirm || !fingerprint) return;
    active.current = true; setBusy(true); setError("");
    try {
      await maintenance("execute", { fingerprint, confirmation, services_stopped: stopped, maintenance_password: password });
      await protection.clear();
      knownOperation.current = true; setPassword(""); setDialog(false);
      setStatus({ fingerprint, status: "running", phase: "validating" });
      if (!standalone) window.location.assign("/maintenance");
    } catch (e) { const code = e instanceof Error ? e.message : "network"; setError(errors[code] || `请求结果尚未确认（${code}）。请核对执行状态；再次提交会复用同一维护记录。`); }
    finally { active.current = false; setBusy(false); }
  }
  const close = useCallback(() => { if (!busy) { setDialog(false); setPassword(""); setConfirmation(""); setStopped(false); } }, [busy]);
  return <div className="space-y-5">
    {standalone && <nav className="text-sm"><Link to="/login" state={{ from: "/maintenance" }} className="text-primary">管理员登录</Link><span className="mx-3">·</span><Link to="/admin" className="text-primary">返回管理员系统</Link></nav>}
    <SectionHeader title="系统维护" description="全站清空使用同一受保护流程：管理员身份、独立维护密码、目标确认和停写锁。" />
    <Card className="border-red-200"><h2 className="text-lg font-semibold text-danger">全清空业务数据</h2><p className="mt-2 text-sm leading-6">清除全部管理员和用户、批改任务、教材与题库、作答结果、上传及孤立文件、BYOK 配置、会话与邮件记录、后台队列、黑名单、业务配置、配额计数、运营事件和应用内审计。用户名和邮箱重新释放；旧任务、旧会话及旧文件引用失效。</p><p className="mt-3 text-sm leading-6">保留数据库结构、部署配置、密钥和基础设施。业务规则恢复为部署环境变量或代码默认值。文件删除不可撤回；外部备份、第三方供应商副本不在自动清理范围。</p></Card>
    {status ? <Card className="space-y-3"><h2 className="font-semibold">维护执行状态</h2><p role="status">{status.status === "completed" ? "清理完成：数据库及项目存储残留检查已通过。" : status.status === "running" ? `正在执行：${status.phase === "database" ? "清理数据库" : "核对或清理存储"}${status.files_remaining != null ? `，剩余本地文件 ${status.files_remaining}` : ""}` : "清理未完成。保持普通服务停止，使用同一记录安全重试。"}</p><p className="break-all text-xs text-muted-foreground">维护记录：{status.fingerprint}</p>{status.error_code && <p role="alert" className="text-danger">失败位置：{status.error_code}</p>}{status.status === "completed" ? <p className="text-sm leading-6">所有管理员已清除。停止维护进程，在私有终端运行 scripts/create_admin.py 初始化首位管理员；核对初始注册规则后，重启服务以丢弃旧缓存。维护回执在独立维护目录中，不保留个人资料或凭据。</p> : status.status !== "running" && <Button variant="danger" onClick={() => { setDialog(true); setError(""); }}>继续此维护记录</Button>}</Card> : <Card><div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">清理范围预览</h2><Button variant="secondary" disabled={preview.isFetching} onClick={() => void preview.refetch()}>{preview.isFetching ? "正在核对…" : "刷新清理预览"}</Button></div>
      {preview.isError ? <p role="alert" className="mt-4 text-danger">预览读取失败，请重试或重新登录。未执行任何清理。</p> : preview.isLoading ? <p role="status" className="mt-4">正在读取清理范围…</p> : !p?.available ? <p className="mt-4 text-sm leading-6">尚未验证项目专用存储与维护目录，无法执行清理。检查项：{p?.reason}。</p> : <>
        <dl className="mt-5 grid grid-cols-2 gap-4 sm:grid-cols-4">{[["环境", p.environment], ["全部账号", p.tables?.users ?? 0], ["数据库记录", Object.values(p.tables ?? {}).reduce((a, b) => a + b, 0)], ["本地文件", p.storage?.files ?? 0], ["对象版本及标记", p.storage?.versions_and_markers ?? 0], ["未完成分段上传", p.storage?.multipart_uploads ?? 0], ["已盘点存储", `${((p.storage?.bytes ?? 0) / 1048576).toFixed(2)} MiB`]].map(([name, value]) => <div key={name}><dt className="text-sm text-muted-foreground">{name}</dt><dd className="mt-1 text-lg font-semibold">{value}</dd></div>)}</dl>
        <p className="mt-4 break-all text-sm">数据库标识：{p.target?.database}；对象存储专用范围：{p.target?.object_scope || "未使用"}</p><ul className="mt-2 space-y-1 break-all text-sm">{p.target?.local_roots.map(root => <li key={root}>本地目录：{root}</li>)}</ul>
        <p className="mt-4 break-all text-xs text-muted-foreground">预览编号：{p.fingerprint}</p>
        <Button className="mt-5" variant="danger" disabled={!p.execution_available} onClick={() => { setDialog(true); setError(""); }}>全清空并恢复全新状态</Button>
        {!p.execution_available && <p className="mt-3 text-sm leading-6">当前普通管理服务仅提供预览。请停止公共、私有服务及所有后台任务，使用相同配置启动回环地址的专用维护服务，并配置独立维护密码哈希。随后访问 <a href="/maintenance" className="text-primary">系统维护</a>。按钮不可用的原因：普通服务还未退出，或维护密码尚未配置。</p>}
      </>}
    </Card>}
    {error && !dialog && <p role="alert" className="text-danger">{error}</p>}
    <Card><h2 className="font-semibold">安全执行条件</h2><p className="mt-3 text-sm leading-6">专用维护服务只绑定回环地址，通过 SSH 隧道或 VPN 访问，单进程运行；不要开放公网。所有写入服务与调度器必须停止，共享维护锁与数据库独占锁会再次检查。S3 仅允许已标记的本项目独占桶，清除全部版本、删除标记及未完成上传；未知范围或删除残留都会使操作失败。外部备份的处理需要单独确认。</p></Card>
    {dialog && <LibraryDialog title={status ? "继续受保护的全站清空" : "确认全清空业务数据"} description="这会删除包括您自己的管理员账号在内的全部业务数据，无法撤回。" closeLabel="关闭清空确认" onClose={close} footer={<><Button variant="secondary" disabled={busy} onClick={() => protection.requestLeave(close)}>取消</Button><Button variant="danger" disabled={!canConfirm} onClick={() => void execute()}>{busy ? "正在验证并启动…" : "确认全清空"}</Button></>}>
      <div className="space-y-4"><p className="break-all text-sm">目标：{p?.target?.database || fingerprint}</p><label className="grid gap-2 text-sm">独立维护密码<input type="password" autoComplete="off" className="h-10 rounded-md border bg-background px-3" maxLength={128} value={password} disabled={busy} onChange={e => setPassword(e.target.value)} /></label><label className="grid gap-2 text-sm">输入确认短语<span className="break-all select-all text-xs">{phrase}</span><input className="h-10 rounded-md border bg-background px-3" autoComplete="off" value={confirmation} disabled={busy} onChange={e => setConfirmation(e.target.value)} /></label><label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={stopped} disabled={busy} onChange={e => setStopped(e.target.checked)} />已停止全部公共、私有服务、worker、调度器和旧容器。</label><p className="text-xs text-muted-foreground">按钮需填写正确短语、维护密码并确认停止服务。密码不会写入草稿或审计。</p>{error && <p role="alert" className="text-danger">{error}</p>}</div>
    </LibraryDialog>}
  </div>;
}
