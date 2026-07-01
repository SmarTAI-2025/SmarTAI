import { ArrowRight, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { normalizeAPIError } from "@/api/client";
import { useDeleteTask, useTasks } from "@/api/hooks";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import type { TaskLite, TaskStatus } from "@/types";

const statusMeta: Record<TaskStatus, { label: string; badgeClassName: string }> = {
  draft: {
    label: "草稿",
    badgeClassName: "border-slate-300 bg-slate-100 text-slate-700 dark:border-slate-500/40 dark:bg-slate-400/10 dark:text-slate-200",
  },
  extracting_problems: {
    label: "题目解析中",
    badgeClassName: "border-primary/30 bg-primary/10 text-primary",
  },
  problems_ready: {
    label: "题目就绪",
    badgeClassName: "border-accent/30 bg-accent/10 text-accent",
  },
  parsing_submissions: {
    label: "作答解析中",
    badgeClassName: "border-primary/30 bg-primary/10 text-primary",
  },
  submissions_ready: {
    label: "作答就绪",
    badgeClassName: "border-accent/30 bg-accent/10 text-accent",
  },
  grading: {
    label: "批改中",
    badgeClassName: "border-warning/30 bg-warning/10 text-warning",
  },
  graded: {
    label: "已完成",
    badgeClassName: "border-accent/30 bg-accent text-white",
  },
  error: {
    label: "异常",
    badgeClassName: "border-danger/30 bg-danger/10 text-danger",
  },
};

export function HistoryPage() {
  const tasksQuery = useTasks();
  const deleteTask = useDeleteTask();
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);
  const tasks = useMemo(() => toSortedTasks(tasksQuery.data), [tasksQuery.data]);
  const errorMessage = tasksQuery.error ? normalizeAPIError(tasksQuery.error).message : null;

  async function handleDelete(task: TaskLite) {
    const confirmed = window.confirm(`确定删除“${task.name}”吗？此操作会删除该任务及相关批改结果。`);
    if (!confirmed) {
      return;
    }

    setDeletingTaskId(task.task_id);
    try {
      await deleteTask.mutateAsync(task.task_id);
      toast.success("任务已删除。");
    } catch (error) {
      toast.error(normalizeAPIError(error).message);
    } finally {
      setDeletingTaskId(null);
    }
  }

  return (
    <div className="grid gap-5">
      <SectionHeader
        title="历史任务"
        description="查看当前教师的草稿、进行中和已完成批改任务。"
        action={
          <Link to="/tasks/new">
            <Button>
              <Plus className="h-4 w-4" />
              新建任务
            </Button>
          </Link>
        }
      />
      <Card>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h2 className="text-base font-semibold">全部任务</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {tasksQuery.isLoading ? "正在加载任务..." : `共 ${tasks.length} 个任务`}
            </p>
          </div>
          <Button
            type="button"
            variant="ghost"
            className="w-fit"
            disabled={tasksQuery.isFetching}
            onClick={() => void tasksQuery.refetch()}
          >
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
        </div>
        <div className="mt-4">
          {tasksQuery.isLoading ? <HistoryLoading /> : null}
          {!tasksQuery.isLoading && errorMessage ? (
            <EmptyState
              title="无法加载历史任务"
              description={errorMessage}
              action={
                <Button type="button" variant="secondary" onClick={() => void tasksQuery.refetch()}>
                  <RefreshCw className="h-4 w-4" />
                  重试
                </Button>
              }
            />
          ) : null}
          {!tasksQuery.isLoading && !errorMessage && tasks.length === 0 ? (
            <EmptyState
              title="暂无历史任务"
              description="创建一个批改任务后，这里会保留草稿、进行中任务和结果入口。"
              action={
                <Link to="/tasks/new">
                  <Button variant="secondary">创建任务</Button>
                </Link>
              }
            />
          ) : null}
          {!tasksQuery.isLoading && !errorMessage && tasks.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] border-separate border-spacing-0 text-left text-sm">
                <thead>
                  <tr className="text-xs text-muted-foreground">
                    <th className="border-b px-3 py-2 font-medium">任务</th>
                    <th className="border-b px-3 py-2 font-medium">状态</th>
                    <th className="border-b px-3 py-2 font-medium">题目/学生</th>
                    <th className="border-b px-3 py-2 font-medium">本任务资料</th>
                    <th className="border-b px-3 py-2 font-medium">更新时间</th>
                    <th className="border-b px-3 py-2 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((task) => {
                    const meta = statusMeta[task.status];
                    const isDeleting = deleteTask.isPending && deletingTaskId === task.task_id;

                    return (
                      <tr key={task.task_id} className="align-top">
                        <td className="border-b px-3 py-3">
                          <div className="font-medium">{task.name}</div>
                          <code className="mt-1 block text-xs text-muted-foreground">{task.task_id}</code>
                          {task.status === "error" && task.error ? (
                            <p className="mt-2 text-xs leading-5 text-danger">{task.error}</p>
                          ) : null}
                        </td>
                        <td className="border-b px-3 py-3">
                          <span className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${meta.badgeClassName}`}>
                            {meta.label}
                          </span>
                        </td>
                        <td className="border-b px-3 py-3 text-muted-foreground">
                          {task.problem_count} / {task.student_count}
                        </td>
                        <td className="border-b px-3 py-3 text-muted-foreground">{task.kb_doc_count}</td>
                        <td className="border-b px-3 py-3 text-muted-foreground">{formatTaskTime(task.updated_at)}</td>
                        <td className="border-b px-3 py-3">
                          <div className="flex justify-end gap-2">
                            <Link to={taskDestination(task)}>
                              <Button type="button" variant="secondary" className="h-8">
                                打开
                                <ArrowRight className="h-4 w-4" />
                              </Button>
                            </Link>
                            <Button
                              type="button"
                              variant="danger"
                              className="h-8"
                              disabled={deleteTask.isPending}
                              onClick={() => void handleDelete(task)}
                            >
                              <Trash2 className="h-4 w-4" />
                              {isDeleting ? "删除中..." : "删除"}
                            </Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </Card>
    </div>
  );
}

function HistoryLoading() {
  return (
    <div className="grid gap-2">
      {Array.from({ length: 5 }).map((_, index) => (
        <div key={index} className="grid gap-2 rounded-md border p-3">
          <div className="h-4 w-1/3 rounded bg-muted" />
          <div className="h-3 w-1/4 rounded bg-muted" />
        </div>
      ))}
    </div>
  );
}

function toSortedTasks(data: Record<string, TaskLite> | undefined): TaskLite[] {
  return Object.values(data ?? {}).sort((a, b) => b.updated_at - a.updated_at);
}

function taskDestination(task: TaskLite): string {
  switch (task.status) {
    case "extracting_problems":
    case "problems_ready":
      return `/tasks/${task.task_id}/upload/problems`;
    case "parsing_submissions":
    case "submissions_ready":
      return `/tasks/${task.task_id}/upload/submissions`;
    case "grading":
    case "graded":
      return `/tasks/${task.task_id}/results`;
    case "draft":
    case "error":
    default:
      return `/tasks/${task.task_id}/setup`;
  }
}

function formatTaskTime(timestamp: number): string {
  if (!Number.isFinite(timestamp) || timestamp <= 0) {
    return "—";
  }

  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp * 1000));
}
