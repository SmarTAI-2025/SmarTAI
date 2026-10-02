import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { useBlocker, useSearchParams } from "react-router-dom";
import { adminListUsers } from "@/api/admin";
import { getBusinessConfig, saveBusinessConfig, type BusinessConfiguration, type BusinessConfigKey, type BusinessConfigUpdate } from "@/api/adminBusinessConfig";
import { normalizeAPIError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";

const labels: Record<BusinessConfigKey, string> = {
  allowed_email_domains: "允许注册的邮箱域名",
  email_verification_resend_seconds: "重发冷却（秒）",
  email_verification_hourly_email_limit: "每邮箱每小时上限",
  email_verification_hourly_ip_limit: "每 IP 每小时上限",
  unfinished_source_quota_bytes: "未完成任务原件配额（字节）",
  knowledge_storage_quota_bytes: "个人知识资料配额（字节）",
};
const sources = { user_override: "该用户覆盖", global_override: "全局覆盖", settings: "部署设置", default: "代码默认" };
const storageKeys: BusinessConfigKey[] = ["unfinished_source_quota_bytes", "knowledge_storage_quota_bytes"];
const groups: Array<{ title: string; hint: string; keys: BusinessConfigKey[] }> = [
  { title: "注册规则", hint: "仅影响后续注册；待验证申请在完成验证时按当时规则检查。已有账号保持可用。", keys: ["allowed_email_domains"] },
  { title: "邮件发送限制", hint: "注册、重发与找回密码使用相同设置，沿用各自已有计数。修改不清空当前小时次数，也不改写已有链接的过期时间。", keys: ["email_verification_resend_seconds", "email_verification_hourly_email_limit", "email_verification_hourly_ip_limit"] },
  { title: "用户存储配额", hint: "每人分别计算两类占用。512 MiB = 536870912 字节。0 表示禁止新增占用；降低配额不删除已有数据，超过额度时仍可释放空间。", keys: storageKeys },
];
type Draft = Partial<Record<BusinessConfigKey, { inherit: boolean; text: string }>>;
function initialDraft(config: BusinessConfiguration): Draft {
  return Object.fromEntries(Object.entries(config.fields).map(([key, field]) => [key, { inherit: field.override === null, text: String(field.override ?? field.effective) }]));
}
function displayValue(key: BusinessConfigKey, value: string | number) {
  if (key === "allowed_email_domains") return value === "" ? "全部拒绝" : value === "*" ? "允许全部域名" : String(value);
  return storageKeys.includes(key) ? `${Number(value).toLocaleString()} 字节（${(Number(value) / 1048576).toLocaleString(undefined, { maximumFractionDigits: 2 })} MiB）` : String(value);
}

function ConfigEditor({ initial, reload, onDirty, onSaved }: { initial: BusinessConfiguration; reload: () => Promise<BusinessConfiguration>; onDirty: (dirty: boolean) => void; onSaved?: () => void }) {
  const [snapshot, setSnapshot] = useState(initial);
  const [draft, setDraft] = useState<Draft>(() => initialDraft(initial));
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const active = useRef(false);
  const retry = useRef<{ serialized: string; key: string } | null>(null);
  const userScope = snapshot.scope === "user";
  const changedKeys = (Object.keys(snapshot.fields) as BusinessConfigKey[]).filter(key => {
    const field = snapshot.fields[key]!, item = draft[key]!;
    return item.inherit ? field.override !== null : field.override === null || item.text !== String(field.override);
  });
  const dirty = changedKeys.length > 0;
  useEffect(() => { onDirty(dirty || busy); return () => onDirty(false); }, [dirty, busy, onDirty]);

  function change(key: BusinessConfigKey, value: { inherit: boolean; text: string }) {
    setDraft(previous => ({ ...previous, [key]: value })); setMessage("");
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (active.current || !dirty || conflict) return;
    setError(""); setMessage("");
    if (!reason.trim()) { setError("请填写修改原因。"); return; }
    const changes: BusinessConfigUpdate["changes"] = {};
    for (const key of changedKeys) {
      const item = draft[key]!, bounds = snapshot.fields[key]!.bounds;
      if (item.inherit) changes[key] = null;
      else if (key === "allowed_email_domains") changes[key] = item.text;
      else {
        const number = Number(item.text);
        if (!/^\d+$/.test(item.text) || !Number.isSafeInteger(number) || (bounds && (number < bounds[0] || number > bounds[1]))) {
          setError(`${labels[key]}须为 ${bounds?.[0]}–${bounds?.[1]} 之间的整数；恢复继承请使用按钮。`); return;
        }
        changes[key] = number;
      }
    }
    const input: BusinessConfigUpdate = { expected_version: snapshot.version, changes, reason: reason.trim(), ...(userScope ? { expected_global_version: snapshot.global_version } : {}) };
    const serialized = JSON.stringify(input);
    if (retry.current?.serialized !== serialized) retry.current = { serialized, key: crypto.randomUUID() };
    active.current = true; setBusy(true);
    try {
      const result = await saveBusinessConfig(input, retry.current.key, snapshot.owner_id ?? undefined);
      setSnapshot(result); setDraft(initialDraft(result)); setReason(""); retry.current = null;
      setMessage("配置已保存，后续请求使用新设置。"); onSaved?.();
    } catch (failure) {
      const status = normalizeAPIError(failure).status;
      if (status === 409) { setConflict(true); setError("配置发生冲突，可能已被其他管理员修改。请放弃更改并载入最新配置，核对后重新保存。"); }
      else if (status === 422) setError("配置值无效。请检查域名格式与数字范围；域名只填根域名，* 必须单独使用。");
      else if (status === 403 || status === 401) setError("管理员权限已变化，请重新登录核对。");
      else setError("暂时无法确认是否保存。当前修改已保留，再次保存会安全复用本次请求。");
    } finally { active.current = false; setBusy(false); }
  }
  async function refresh() {
    if (active.current) return;
    active.current = true; setBusy(true); setError(""); setMessage("");
    try { const result = await reload(); setSnapshot(result); setDraft(initialDraft(result)); setReason(""); setConflict(false); retry.current = null; }
    catch { setError("重新载入失败，当前修改仍保留，请重试。"); }
    finally { active.current = false; setBusy(false); }
  }
  const visibleGroups = userScope ? [{ ...groups[2], title: "该用户的存储覆盖" }] : groups;
  return <form onSubmit={event => void submit(event)} className="space-y-5" aria-label={userScope ? "用户配额表单" : "全局配置表单"}>
    {visibleGroups.map(group => <Card key={group.title} id={!userScope ? ({ "注册规则": "registration-rules", "邮件发送限制": "mail-limits", "用户存储配额": "default-quotas" } as Record<string, string>)[group.title] : undefined} className="scroll-mt-6 space-y-5">
      <div><h2 className="text-lg font-semibold">{group.title}</h2><p className="mt-2 text-sm text-muted-foreground">{group.hint}</p></div>
      {group.keys.map(key => {
        const field = snapshot.fields[key]!, item = draft[key]!, usage = snapshot.usage?.[key];
        return <div key={key} className="space-y-2 border-t pt-4">
          <label className="grid gap-2 text-sm font-medium"><span>{labels[key]}</span>
            <input className="h-10 w-full rounded-md border bg-background px-3 font-normal disabled:bg-muted" value={item.text} disabled={item.inherit || busy} inputMode={key === "allowed_email_domains" ? "text" : "numeric"} maxLength={key === "allowed_email_domains" ? 4096 : 16} onChange={e => change(key, { inherit: false, text: e.target.value })} />
          </label>
          <p className="break-words text-sm text-muted-foreground">当前生效：{displayValue(key, field.effective)} · 来源：{sources[field.source]}</p>
          {usage && <p className="text-sm">已占用 {usage.used_bytes.toLocaleString()} 字节（含预留 {usage.reserved_bytes.toLocaleString()} 字节）{usage.used_bytes > usage.limit_bytes ? "，已超过当前配额，新增占用将被拒绝。" : "。"}</p>}
          {field.bounds && <p className="text-xs text-muted-foreground">允许范围：{field.bounds[0].toLocaleString()}–{field.bounds[1].toLocaleString()}。</p>}
          <div className="flex flex-wrap items-center gap-2"><Button disabled={busy} variant="secondary" onClick={() => change(key, { inherit: !item.inherit, text: String(field.effective) })}>{item.inherit ? `设置${userScope ? "用户" : "全局"}覆盖` : "恢复继承"}</Button><span className="text-xs text-muted-foreground">{item.inherit ? `保存后继承${userScope ? "全局配置或部署默认值" : "部署默认值"}` : "保存后使用此覆盖值"}</span></div>
          {key === "allowed_email_domains" && <div className="space-y-1 text-xs text-muted-foreground"><p>多个根域名用英文逗号分隔。example.edu 同时允许 @example.edu、@mail.example.edu；拒绝 @badexample.edu 和 @example.edu.evil。</p><p>空字符串明确表示全部拒绝；单独 * 表示全部允许。恢复继承请使用按钮。域名修改不封禁已有账号，后台管理域名后找回密码独立于注册域名。</p></div>}
        </div>;
      })}
      {group.title === "邮件发送限制" && <p className="text-xs text-muted-foreground">新邮件采用新冷却时间；已发注册邮件保留已有冷却截止时间。验证链接有效期：{snapshot.read_only?.email_verification_expiry_seconds ?? 1800} 秒（只读）。管理员协助找回密码也受上述限制。</p>}
    </Card>)}
    <Card className="space-y-3">
      <label className="grid gap-2 text-sm">修改原因<input className="h-10 rounded-md border bg-background px-3" maxLength={80} value={reason} disabled={busy} onChange={e => setReason(e.target.value)} placeholder="填写本次调整原因，记录到审计日志" /></label>
      {dirty && <p role="status" className="text-sm text-amber-700">有未保存的修改。</p>}
      {message && <p role="status" className="text-sm">{message}</p>}
      {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      <div className="flex flex-wrap gap-2"><Button type="submit" disabled={!dirty || busy || conflict || !reason.trim()}>{busy ? "处理中…" : userScope ? "保存用户配额" : "保存全局配置"}</Button><Button variant="secondary" disabled={busy} onClick={() => void refresh()}>{dirty || conflict ? "放弃更改并载入最新" : "刷新配置"}</Button></div>
    </Card>
  </form>;
}

export function AdminBusinessConfigPage() {
  const [params] = useSearchParams();
  const requestedOwner = params.get("userId") ?? params.get("user") ?? "";
  const positionedOwner = useRef("");
  const [ownerId, setOwnerId] = useState(params.get("userId") ?? params.get("user") ?? "");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [globalDirty, setGlobalDirty] = useState(false);
  const [userDirty, setUserDirty] = useState(false);
  const [userEpoch, setUserEpoch] = useState(0);
  const dirty = globalDirty || userDirty;
  const blocker = useBlocker(dirty);
  useEffect(() => {
    if (!dirty) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", protect); return () => window.removeEventListener("beforeunload", protect);
  }, [dirty]);
  const global = useQuery({ queryKey: ["admin-business-config"], queryFn: ({ signal }) => getBusinessConfig(undefined, signal), refetchOnWindowFocus: false, gcTime: 0, retry: false });
  const userConfig = useQuery({ queryKey: ["admin-business-config", ownerId, userEpoch], queryFn: ({ signal }) => getBusinessConfig(ownerId, signal), enabled: !!ownerId, refetchOnWindowFocus: false, gcTime: 0, retry: false });
  useEffect(() => { setOwnerId(requestedOwner); }, [requestedOwner]);
  useEffect(() => {
    if (!requestedOwner) { positionedOwner.current = ""; return; }
    if (!global.isPending && !userConfig.isPending && positionedOwner.current !== requestedOwner) {
      document.getElementById("user-quotas")?.scrollIntoView?.({ block: "start" });
      positionedOwner.current = requestedOwner;
    }
  }, [requestedOwner, global.isPending, userConfig.isPending]);
  const users = useQuery({ queryKey: ["admin-business-config-users", query], queryFn: () => adminListUsers({ search: query || undefined, page: 1, page_size: 25 }), retry: false });
  const items = Array.isArray(users.data) ? users.data : users.data?.items ?? [];
  return <div className="max-w-4xl space-y-6">
    <SectionHeader title="业务配置管理" description="调整注册、邮件与个人存储规则。优先使用用户覆盖，其次全局覆盖，最后继承部署设置或代码默认值。" />
    <nav aria-label="配置项目" className="flex flex-wrap gap-2 text-sm">{[["registration-rules", "注册规则"], ["mail-limits", "邮件限制"], ["default-quotas", "默认配额"], ["user-quotas", "单用户配额"]].map(([id, label]) => <a key={id} href={`#${id}`} className="rounded-md border bg-card px-3 py-2 text-primary">{label}</a>)}</nav>
    {blocker.state === "blocked" && <Card role="alert" className="space-y-3"><p>还有未保存的修改，离开会丢弃本页编辑。</p><div className="flex gap-2"><Button onClick={() => blocker.reset()}>保留编辑</Button><Button variant="secondary" onClick={() => blocker.proceed()}>放弃更改并离开</Button></div></Card>}
    {global.isPending ? <p role="status">正在载入全局配置…</p> : global.isError ? <Card role="alert">配置加载失败。<Button onClick={() => void global.refetch()}>重试</Button></Card> : <ConfigEditor initial={global.data} onDirty={setGlobalDirty} onSaved={() => { if (!userDirty) setUserEpoch(n => n + 1); }} reload={async () => { const result = await global.refetch(); if (!result.data || result.error) throw new Error("load"); return result.data; }} />}
    <section id="user-quotas" aria-label="单独用户配额" className="scroll-mt-6 space-y-4">
      <Card className="space-y-4"><h2 className="text-lg font-semibold">为单个用户设置存储配额</h2><p className="text-sm text-muted-foreground">只覆盖此用户的两类存储上限；留在继承状态的项目会随全局配置更新。</p>
        <form onSubmit={e => { e.preventDefault(); setQuery(search.trim()); }} className="flex flex-wrap items-end gap-2"><label className="grid flex-1 gap-2 text-sm">搜索用户名或邮箱<input className="h-10 rounded-md border bg-background px-3" value={search} maxLength={128} onChange={e => setSearch(e.target.value)} /></label><Button type="submit">搜索</Button></form>
        {users.isPending ? <p role="status">用户加载中…</p> : users.isError ? <p role="alert">用户加载失败。<Button onClick={() => void users.refetch()}>重试用户列表</Button></p> : <label className="grid gap-2 text-sm">选择用户<select className="h-10 w-full rounded-md border bg-background px-3" value={ownerId} disabled={userDirty} onChange={e => setOwnerId(e.target.value)}><option value="">请选择用户</option>{ownerId && !items.some(u => u.id === ownerId) && <option value={ownerId}>已选用户：{ownerId}</option>}{items.map(u => <option key={u.id} value={u.id}>{u.username} · {u.email || "无邮箱"}</option>)}</select></label>}
        {!users.isPending && !users.isError && !items.length && <p className="text-sm">没有匹配的用户。</p>}
        {users.data && !Array.isArray(users.data) && users.data.has_next && <p className="text-xs text-muted-foreground">仅列出前 25 项，请输入更精确的用户名或邮箱。</p>}
        {userDirty && <p className="text-sm text-amber-700">请先保存或放弃此用户的修改，再选择其他用户。</p>}
      </Card>
      {ownerId && (userConfig.isPending ? <p role="status">正在载入用户配额…</p> : userConfig.isError ? <Card role="alert">用户配额加载失败，账号可能已销户。<Button onClick={() => void userConfig.refetch()}>重试用户配额</Button></Card> : <ConfigEditor key={`${ownerId}:${userEpoch}`} initial={userConfig.data} onDirty={setUserDirty} reload={async () => { const result = await userConfig.refetch(); if (!result.data || result.error) throw new Error("load"); return result.data; }} />)}
    </section>
    {global.data?.read_only && <Card className="space-y-2 text-sm text-muted-foreground"><h2 className="font-semibold text-foreground">模型额度（只读）</h2><p>共享池每日请求：{global.data.read_only.shared_pool_daily_request_limit}；估算 token：{global.data.read_only.shared_pool_daily_estimated_token_limit}；历史任务 Ask 每日调用：{global.data.read_only.history_query_llm_daily_limit}。</p><p>{global.data.read_only.model_quota_note}</p></Card>}
  </div>;
}
