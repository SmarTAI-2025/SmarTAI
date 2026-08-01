import { useCurrentUser } from "@/api/hooks";
import { Card } from "@/components/ui/Card";
import { useI18n } from "@/i18n/I18nProvider";

/**
 * Minimal admin landing surface (Task 9 foundation). Task 10 replaces this with
 * AdminUsersPage / AdminInvitesPage / AdminSystemPage; until then it confirms the
 * role guard routes an admin here and shows the role + workspace label.
 */
export function AdminPlaceholder() {
  const user = useCurrentUser();
  const { t } = useI18n();
  return (
    <main className="mx-auto max-w-3xl px-4 py-10">
      <Card className="p-6">
        <h1 className="text-lg font-semibold">{t("dashboard")}</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {user.data ? `${user.data.role} · ${user.data.username}` : ""}
        </p>
        <p className="mt-4 text-sm text-muted-foreground">
          管理员工作区（用户 / 邀请 / 系统）将在 Task 10 实现。
        </p>
      </Card>
    </main>
  );
}
