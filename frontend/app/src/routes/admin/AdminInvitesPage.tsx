import { useState } from "react";
import { useAdminCreateInvite, useAdminInvites } from "@/api/hooks/admin";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { EmptyState } from "@/components/ui/EmptyState";

/**
 * Admin invitations: create teacher/student/admin invites (optionally pre-bind a
 * course for student invites) and list outstanding + used codes. The created
 * code is shown once for the admin to copy and send privately.
 */
export function AdminInvitesPage() {
  const invites = useAdminInvites();
  const createInvite = useAdminCreateInvite();
  const [role, setRole] = useState<"teacher" | "student" | "admin">("student");
  const [email, setEmail] = useState("");
  const [lastCode, setLastCode] = useState<string | null>(null);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    createInvite.mutate(
      { role, email: email || undefined },
      {
        onSuccess: (data) => {
          setLastCode(data.invite_code);
          setEmail("");
        },
      },
    );
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <SectionHeader title="邀请管理" description="创建教师 / 学生 / 管理员邀请码并查看使用情况。" />
      <Card>
        <form className="grid gap-4 sm:grid-cols-[auto_1fr_auto]" onSubmit={submit}>
          <Field label="角色">
            <select
              className="h-9 rounded-md border bg-background px-2 text-sm"
              value={role}
              onChange={(e) => setRole(e.target.value as typeof role)}
            >
              <option value="student">student</option>
              <option value="teacher">teacher</option>
              <option value="admin">admin</option>
            </select>
          </Field>
          <Field label="邮箱（可选）" hint="留空则不绑定邮箱。">
            <Input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="user@example.com" />
          </Field>
          <div className="flex items-end">
            <Button type="submit" disabled={createInvite.isPending}>
              生成邀请
            </Button>
          </div>
        </form>
        {lastCode ? (
          <p className="mt-4 rounded-md border bg-muted/40 p-3 text-sm">
            新邀请码：<code className="font-mono font-semibold">{lastCode}</code>（请私下发送给受邀人）
          </p>
        ) : null}
      </Card>

      <Card className="p-0">
        {invites.isLoading ? (
          <div className="p-6 text-sm text-muted-foreground">加载中...</div>
        ) : invites.isError ? (
          <div className="p-6 text-sm text-danger">加载失败，请稍后重试。</div>
        ) : !invites.data || invites.data.length === 0 ? (
          <EmptyState title="暂无邀请" description="还没有创建任何邀请码。" />
        ) : (
          <table className="w-full text-sm">
            <thead className="border-b text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-2">邀请码</th>
                <th className="px-4 py-2">角色</th>
                <th className="px-4 py-2">邮箱</th>
                <th className="px-4 py-2">状态</th>
              </tr>
            </thead>
            <tbody>
              {invites.data.map((iv) => (
                <tr key={iv.code} className="border-b last:border-0">
                  <td className="px-4 py-2 font-mono">{iv.code}</td>
                  <td className="px-4 py-2">{iv.role}</td>
                  <td className="px-4 py-2 text-muted-foreground">{iv.email || "—"}</td>
                  <td className="px-4 py-2">
                    {iv.used_at ? (
                      <span className="text-muted-foreground">已使用</span>
                    ) : (
                      <span className="text-emerald-600">可用</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
    </div>
  );
}
