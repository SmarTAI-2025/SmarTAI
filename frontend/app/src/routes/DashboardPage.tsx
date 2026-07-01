import { AlertTriangle, ArrowRight, CheckCircle2, CircleDashed, ListChecks, Plus, RefreshCw } from "lucide-react";
import { useMemo } from "react";
import { Link } from "react-router-dom";
import { normalizeAPIError } from "@/api/client";
import { useTasks } from "@/api/hooks";
import { Button } from "@/components/ui/Button";
import { Card, SectionHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { StatTile } from "@/components/ui/StatTile";
import type { TaskLite, TaskStatus } from "@/types";

const activeStatuses = new Set<TaskStatus>([
  "draft",
  "extracting_problems",
  "problems_ready",
  "parsing_submissions",
  "submissions_ready",
  "grading",
]);

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

export function DashboardPage() {
  const tasksQuery = useTasks();
  const tasks = useMemo(() => toSortedTasks(tasksQuery.data), [tasksQuery.data]);
  const recentTasks = tasks.slice(0, 6);
  const activeCount = tasks.filter((task) => activeStatuses.has(task.status)).length;
  const completedCount = tasks.filter((task) => task.status === "graded").length;
  const errorMessage = tasksQuery.error ? normalizeAPIError(tasksQuery.error).message : null;

  return (
    <div className="grid gap-5">
      <SectionHeader
        title="教师工作台"
        description="创建批改任务，上传题目与学生作答，查看 AI 批改和学情分析。"
        action={
          <Link to="/tasks/new">
            <Button>
              <Plus className="h-4 w-4" />
              新建任务
            </Button>
          </Link>
        }
      />
      <div className="grid gap-3 md:grid-cols-3">
        <StatTile icon={CircleDashed} label="进行中/草稿" value={tasksQuery.isLoading ? "—" : activeCount} tone="warning" />
        <StatTile icon={CheckCircle2} label="已完成" value={tasksQuery.isLoading ? "—" : completedCount} tone="accent" />
        <StatTile icon={ListChecks} label="全部任务" value={tasksQuery.isLoading ? "—" : tasks.length} />
      </div>
      <Card>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <h2 className="text-base font-semibold">近期任务</h2>
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
        <div className="mt-3">
          {tasksQuery.isLoading ? <TaskListLoading /> : null}
          {!tasksQuery.isLoading && errorMessage ? (
            <EmptyState
              title="无法加载任务"
              description={errorMessage}
              action={
                <Button type="button" variant="secondary" onClick={() => void tasksQuery.refetch()}>
                  <RefreshCw className="h-4 w-4" />
                  重试
                </Button>
              }
            />
          ) : null}
          {!tasksQuery.isLoading && !errorMessage && recentTasks.length === 0 ? (
            <EmptyState
              title="还没有批改任务"
              description="创建一个任务后，就可以在这里继续配置、上传和查看结果。"
              action={
                <Link to="/tasks/new">
                  <Button variant="secondary">创建第一个任务</Button>
                </Link>
              }
            />
          ) : null}
          {!tasksQuery.isLoading && !errorMessage && recentTasks.length > 0 ? (
            <div className="grid gap-3 lg:grid-cols-2">
              {recentTasks.map((task) => (
                <TaskSummaryCard key={task.task_id} task={task} />
              ))}
            </div>
          ) : null}
        </div>
      </Card>
    </div>
  );
}

function TaskSummaryCard({ task }: { task: TaskLite }) {
  const destination = taskDestination(task);
  const meta = statusMeta[task.status];

  return (
    <div className="grid gap-3 rounded-lg border bg-background p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold">{task.name}</h3>
          <p className="mt-1 text-xs text-muted-foreground">更新于 {formatTaskTime(task.updated_at)}</p>
        </div>
        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-xs font-medium ${meta.badgeClassName}`}>
          {meta.label}
        </span>
      </div>
      {task.status === "error" && task.error ? (
        <div className="flex items-start gap-2 rounded-md border border-danger/30 bg-danger/10 p-2 text-xs leading-5 text-danger">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{task.error}</span>
        </div>
      ) : null}
      <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-3">
        <TaskMetric label="题目" value={task.problem_count} />
        <TaskMetric label="学生" value={task.student_count} />
        <TaskMetric label="本任务资料" value={task.kb_doc_count} />
      </div>
      <div className="flex items-center justify-between gap-3 border-t pt-3">
        <code className="truncate text-xs text-muted-foreground">{task.task_id}</code>
        <Link to={destination}>
          <Button type="button" variant="secondary" className="h-8">
            {taskActionLabel(task.status)}
            <ArrowRight className="h-4 w-4" />
          </Button>
        </Link>
      </div>
    </div>
  );
}

function TaskMetric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md border px-3 py-2">
      <div>{label}</div>
      <div className="mt-1 text-base font-semibold text-foreground">{value}</div>
    </div>
  );
}

function TaskListLoading() {
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      {Array.from({ length: 4 }).map((_, index) => (
        <div key={index} className="grid gap-3 rounded-lg border bg-background p-4">
          <div className="h-4 w-2/3 rounded bg-muted" />
          <div className="h-3 w-1/3 rounded bg-muted" />
          <div className="grid gap-2 sm:grid-cols-3">
            <div className="h-12 rounded-md bg-muted" />
            <div className="h-12 rounded-md bg-muted" />
            <div className="h-12 rounded-md bg-muted" />
          </div>
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

function taskActionLabel(status: TaskStatus): string {
  switch (status) {
    case "extracting_problems":
    case "problems_ready":
      return "查看题目";
    case "parsing_submissions":
    case "submissions_ready":
      return "查看作答";
    case "grading":
      return "查看进度";
    case "graded":
      return "查看结果";
    case "error":
      return "检查任务";
    case "draft":
    default:
      return "继续配置";
  }
}

function formatTaskTime(timestamp: number): string {
  if (!Number.isFinite(timestamp) || timestamp <= 0) {
    return "—";
  }

  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(timestamp * 1000));
}
