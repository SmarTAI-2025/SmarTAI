import { Eye, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { useDeleteTask } from "@/api/hooks";
import { UnsavedChangesDialog } from "@/components/ui/UnsavedChangesDialog";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { userFacingError } from "@/lib/taskActionGuards";
import { getTaskDestination } from "@/lib/taskFlow";
import type { TaskLite } from "@/types";

export function useTaskListDeletion() {
  const { t, locale } = useI18n();
  const mutation = useDeleteTask();
  const [target, setTarget] = useState<TaskLite | null>(null);
  const [lastDeletedId, setLastDeletedId] = useState<string | null>(null);
  async function confirm() {
    if (!target || mutation.isPending) return;
    try {
      await mutation.mutateAsync(target.task_id);
      setLastDeletedId(target.task_id);
      toast.success(t("historyDeleteSuccess"));
    } catch (error) {
      toast.error(userFacingError(error, locale));
    } finally {
      setTarget(null);
    }
  }
  return {
    lastDeletedId,
    pendingId: mutation.isPending ? target?.task_id : undefined,
    request: setTarget,
    dialog: target && <UnsavedChangesDialog title={t("historyDelete")}
      description={`${t("historyDeleteConfirmPrefix")}${target.name}${t("historyDeleteConfirmSuffix")}`}
      stayLabel={locale === "zh-CN" ? "取消" : "Cancel"}
      leaveLabel={mutation.isPending ? t("historyDeletePending") : locale === "zh-CN" ? "确认删除" : "Confirm deletion"}
      saving={mutation.isPending} onStay={() => setTarget(null)} onLeave={() => void confirm()} />,
  };
}

export type TaskListDeletion = ReturnType<typeof useTaskListDeletion>;

export function TaskListActions({ task, deletion }: { task: TaskLite; deletion: TaskListDeletion }) {
  const taskName = task.name;
  const destination = getTaskDestination(task);
  const deleting = deletion.pendingId === task.task_id;
  const { t } = useI18n();
  const openLabel = `${t("historyOpenTask")} · ${taskName}`;
  const deleteLabel = `${t(deleting ? "historyDeletePending" : "historyDelete")} · ${taskName}`;
  const actionClass = "inline-flex size-8 items-center justify-center rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring";
  return (
    <div className="flex shrink-0 items-center gap-0.5">
      <Link to={destination} title={openLabel} aria-label={openLabel} className={cn(actionClass, "text-muted-foreground hover:bg-muted hover:text-primary")}>
        <Eye aria-hidden="true" size={16} />
      </Link>
      <button type="button" title={deleteLabel} aria-label={deleteLabel} disabled={Boolean(deletion.pendingId)}
        onClick={event => {
          // WebKit does not focus buttons on a pointer click. Restore this trigger on cancel.
          event.currentTarget.focus({ preventScroll: true });
          deletion.request(task);
        }}
        className={cn(actionClass, "text-danger hover:bg-rose-50 disabled:opacity-50 dark:hover:bg-rose-950/40")}>
        <Trash2 aria-hidden="true" size={16} />
      </button>
    </div>
  );
}
