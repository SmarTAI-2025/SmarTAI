import { useDraftProtection } from "@/hooks/useDraftProtection";
import { useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useCurrentUser } from "@/api/hooks";
import { apiClient, clearAuthToken, normalizeAPIError } from "@/api/client";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { AuthPasswordInput } from "@/components/auth/AuthFrame";

export function AdminAccountPage() {
  const user = useCurrentUser(); const navigate = useNavigate(); const cache = useQueryClient();
  const [oldPassword, setOld] = useState(""); const [newPassword, setNew] = useState(""); const [confirm, setConfirm] = useState("");
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const active = useRef(false);
  const localDraft = useDraftProtection({ scope: "admin-password", value: { oldPassword, newPassword, confirm }, secret: true, busy, onRestore: () => { setOld(""); setNew(""); setConfirm(""); } });
  async function submit(e: FormEvent) {
    e.preventDefault(); if (active.current) return;
    if (newPassword !== confirm) { setError("两次新密码不一致。"); return; }
    if (newPassword.length < 8 || newPassword.length > 128) { setError("新密码需要 8–128 个字符。"); return; }
    active.current = true; setBusy(true); setError("");
    try {
      await apiClient.post("/auth/password-change", { current_password: oldPassword, new_password: newPassword });
      await localDraft.clear(); clearAuthToken(); cache.clear(); navigate("/login", { replace: true, state: { authError: "密码已更新，所有旧登录已退出。请使用新密码登录。" } });
    } catch (e) { setError(normalizeAPIError(e).status === 400 ? "原密码不正确，或新密码与原密码相同。" : "无法确认密码是否已更新。请重新登录核对或使用忘记密码。"); }
    finally { setOld(""); setNew(""); setConfirm(""); active.current = false; setBusy(false); }
  }
  return <div className="max-w-2xl space-y-5"><SectionHeader title="管理员账号设置" description={`当前账号：${user.data?.username ?? ""}。这里仅管理你正在使用的管理员账号。`} /><Card><h2 className="font-semibold">修改密码</h2><p className="my-3 text-sm text-muted-foreground">修改成功后，包括当前设备在内的全部登录失效。历史批改任务不会删除。</p><form className="grid gap-4" onSubmit={e => void submit(e)}>{[["当前密码", oldPassword, setOld, "current-password"], ["新密码", newPassword, setNew, "new-password"], ["再次输入新密码", confirm, setConfirm, "new-password"]].map(([label, value, setter, auto]) => <label key={String(label)} className="grid gap-2 text-sm">{String(label)}<AuthPasswordInput autoComplete={String(auto)} value={String(value)} disabled={busy} onChange={e => (setter as (v: string) => void)(e.target.value)} showLabel="显示密码" hideLabel="隐藏密码" /></label>)}{error && <p role="alert" className="text-sm text-danger">{error}</p>}<Button type="submit" disabled={busy || !oldPassword || !newPassword || !confirm}>{busy ? "正在更新…" : "更新密码并退出登录"}</Button></form></Card></div>;
}
