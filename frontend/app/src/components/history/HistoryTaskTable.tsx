import { SortableTableHead } from "@/components/ui/SortableTableHead";
import { TaskListActions, type TaskListDeletion } from "./TaskListActions";
import { taskCourseAndTags, TASK_LIST_COLUMNS } from "./historyPresentation";
import { Link } from "react-router-dom";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { getTaskDestination } from "@/lib/taskFlow";
import type { HistoryCourseFacet, TaskHistoryQuery, TaskLite, TaskTag } from "@/types";
import { HistoryTagPopover } from "./HistoryTagPopover";
import {
  formatHistoryTime,
  formatHistoryEta,
  formatSemesterLabel,
  HISTORY_ACTION_KEYS,
  HISTORY_STAGE_KEYS,
  historyStatusTone,
  TAG_TONE_CLASSES,
} from "./historyPresentation";

interface HistoryTaskTableProps {
  tasks: TaskLite[];
  courses: HistoryCourseFacet[];
  tags: TaskTag[];
  isLoading: boolean;
  errorMessage: string | null;
  deletion: TaskListDeletion;
  hasFilters: boolean;
  sort?: TaskHistoryQuery["sort"];
  onFilter: (patch: Partial<TaskHistoryQuery>) => void;
  onRetry: () => void;
  onClear: () => void;
}

export function HistoryTaskTable({
  tasks,
  courses,
  tags,
  isLoading,
  errorMessage,
  deletion,
  hasFilters,
  sort,
  onFilter,
  onRetry,
  onClear,
}: HistoryTaskTableProps) {
  const { t } = useI18n();
  return (
    <section aria-label={t("historyTableRegion")} className="w-full">
      <div className="overflow-visible pb-2 md:overflow-x-auto">
        <div role="table" aria-label={t("historyTableRegion")} aria-busy={isLoading} className="min-w-0 text-left">
          <div role="row" className={cn("grid h-[42px] items-center px-px text-[13px] font-semibold leading-4 text-muted-foreground", TASK_LIST_COLUMNS)}>
            <SortableTableHead as="div" className="px-[14px]"
              direction={sort === "name_asc" ? "asc" : sort === "name_desc" ? "desc" : null}
              onSort={() => onFilter({ sort: sort === "name_asc" ? "name_desc" : "name_asc" })}>{t("historyColumnTask")}</SortableTableHead>
            <div role="columnheader" className="hidden px-[14px] md:block">{t("dashboardColumnCourseTags")}</div>
            <SortableTableHead as="div" className="px-1 text-center"
              direction={sort === "stage_asc" ? "asc" : sort === "stage_desc" ? "desc" : null}
              onSort={() => onFilter({ sort: sort === "stage_asc" ? "stage_desc" : "stage_asc" })}>{t("historyColumnStage")}</SortableTableHead>
            <SortableTableHead as="div" className="hidden px-[14px] md:block"
              direction={sort === "progress_asc" ? "asc" : sort === "progress_desc" ? "desc" : null}
              onSort={() => onFilter({ sort: sort === "progress_asc" ? "progress_desc" : "progress_asc" })}>{t("historyColumnProgress")}</SortableTableHead>
            <SortableTableHead as="div" className="hidden px-[14px] md:block"
              direction={sort === "eta_asc" ? "asc" : sort === "eta_desc" ? "desc" : null}
              onSort={() => onFilter({ sort: sort === "eta_asc" ? "eta_desc" : "eta_asc" })}>{t("historyColumnEta")}</SortableTableHead>
            <SortableTableHead as="div" className="hidden px-[14px] md:block"
              direction={sort === "updated_asc" ? "asc" : sort === "updated_desc" ? "desc" : null}
              onSort={() => onFilter({ sort: sort === "updated_asc" ? "updated_desc" : "updated_asc" })}>{t("historyColumnUpdated")}</SortableTableHead>
            <div role="columnheader" className="hidden px-[14px] md:block">{t("historyColumnNext")}</div>
            <div role="columnheader" className="text-center">{t("historyColumnActions")}</div>
          </div>

          <div role="rowgroup" className="overflow-visible rounded-[8px] border bg-card">
            {isLoading ? <HistoryLoadingRows /> : null}
            {!isLoading && errorMessage ? (
              <HistoryTableMessage
                title={t("historyLoadError")}
                description={errorMessage}
                actionLabel={t("historyRetry")}
                onAction={onRetry}
              />
            ) : null}
            {!isLoading && !errorMessage && tasks.length === 0 ? (
              <HistoryTableMessage
                title={hasFilters ? t("historyNoMatchesTitle") : t("historyEmptyTitle")}
                description={hasFilters ? t("historyNoMatchesDescription") : t("historyEmptyDescription")}
                actionLabel={hasFilters ? t("historyClearFilters") : t("historyCreateTask")}
                actionTo={hasFilters ? undefined : "/tasks/new"}
                onAction={hasFilters ? onClear : undefined}
              />
            ) : null}
            {!isLoading && !errorMessage ? tasks.map((task) => (
              <HistoryTaskRow
                key={task.task_id}
                task={task}
                courses={courses}
                tags={tags}
                deletion={deletion}
              />
            )) : null}
          </div>
        </div>
      </div>
    </section>
  );
}

function HistoryTaskRow({
  task,
  courses,
  tags,
  deletion,
}: {
  task: TaskLite;
  courses: HistoryCourseFacet[];
  tags: TaskTag[];
  deletion: TaskListDeletion;
}) {
  const { locale, t } = useI18n();
  const { course, taskTags } = taskCourseAndTags(task, courses, tags);
  const destination = getTaskDestination(task);
  const mobileMetadata = [
    task.semester_id ? formatSemesterLabel(task.semester_id, t) : null,
    course?.name ?? null,
    ...taskTags.map((tag) => tag.name),
  ].filter((value): value is string => Boolean(value));

  return (
    <div role="row" className={cn("grid min-h-[76px] items-center border-t text-[13px] leading-4 first:border-t-0 hover:bg-slate-50/70 md:min-h-[62px] dark:hover:bg-slate-800/30", TASK_LIST_COLUMNS)}>
      <div role="cell" className="min-w-0 px-[14px] py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Link to={destination} className="min-w-0 truncate font-semibold text-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring" title={task.name}>
            {task.name}
          </Link>
          {task.needs_attention ? <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-rose-500" title={t("historyNeedsAttention")} /> : null}
        </div>
        <div className="mt-1 hidden min-w-0 items-center gap-1 overflow-visible whitespace-nowrap md:flex">
          {task.semester_id ? (
            <span title={formatSemesterLabel(task.semester_id, t)} className="max-w-[132px] truncate rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-600 outline-none hover:border-primary/30 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300">
              {formatSemesterLabel(task.semester_id, t)}
            </span>
          ) : null}
        </div>
        <div className="mt-1 flex min-w-0 items-center gap-1 md:hidden">
          <HistoryTagPopover task={task} tags={tags} />
          <span title={mobileMetadata.join(" · ")} className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">
            {mobileMetadata.length ? mobileMetadata.join(" · ") : t("historyMetadataUnset")}
          </span>
        </div>
        <p className="mt-1 text-[11px] text-muted-foreground md:hidden">{formatHistoryTime(task.updated_at, locale)}</p>
      </div>

      <div role="cell" className="hidden min-w-0 flex-wrap items-center gap-1 px-[14px] md:flex">
          {course ? (
            <span title={course.name} className="max-w-[100px] truncate rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700 outline-none hover:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring dark:border-blue-800 dark:bg-blue-950/50 dark:text-blue-200">
              {course.name}
            </span>
          ) : null}
          {taskTags.slice(0, 2).map((tag) => (
            <span key={tag.id} title={tag.name} className={cn("max-w-[82px] truncate rounded-full border px-2 py-0.5 text-[11px] outline-none focus-visible:ring-2 focus-visible:ring-ring", TAG_TONE_CLASSES[tag.color])}>
              {tag.name}
            </span>
          ))}
          {taskTags.length > 2 ? <span className="text-[11px] text-muted-foreground">+{taskTags.length - 2}</span> : null}
          {!course && taskTags.length === 0 ? <span className="truncate text-[11px] text-muted-foreground">{t("dashboardCourseUnset")}</span> : null}
          <HistoryTagPopover task={task} tags={tags} />
      </div>

      <div role="cell" className="min-w-0 px-1 py-2 text-center">
        <Link to={destination} title={t(HISTORY_STAGE_KEYS[task.status])} className={cn("inline-flex min-h-7 max-w-full items-center justify-center rounded-full px-3 py-1 text-[13px] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring md:h-7 md:py-0", historyStatusTone(task.status))}>
          <span className="min-w-0 break-words md:truncate">{t(HISTORY_STAGE_KEYS[task.status])}</span>
        </Link>
        <div className="mt-1 flex min-w-0 items-center gap-1 md:hidden">
          <Link to={destination} className="min-w-0 flex-1 truncate text-[11px] font-medium text-muted-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring">
            {t(HISTORY_ACTION_KEYS[task.status])}
          </Link>
        </div>
      </div>

      <HistoryProgressCell task={task} />
      <div role="cell" className="hidden px-[14px] tabular-nums text-muted-foreground md:block">{formatHistoryEta(task.eta_seconds, locale)}</div>
      <div role="cell" className="hidden px-[14px] tabular-nums text-muted-foreground md:block">{formatHistoryTime(task.updated_at, locale)}</div>
      <div role="cell" className="hidden min-w-0 items-center gap-1 px-[14px] md:flex">
          <Link to={destination} title={t(HISTORY_ACTION_KEYS[task.status])} className="min-w-0 flex-1 truncate font-medium text-muted-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring">
          {t(HISTORY_ACTION_KEYS[task.status])}
        </Link>
      </div>
      <div role="cell" className="flex justify-center">
        <TaskListActions task={task} deletion={deletion} />
      </div>
    </div>
  );
}

function HistoryProgressCell({ task }: { task: TaskLite }) {
  const { t } = useI18n();
  let value = task.progress_percent != null && Number.isFinite(task.progress_percent) ? `${task.progress_percent}%` : "—";
  if (task.status === "draft") value = t("historyProgressPending");
  if (["graded", "finalized"].includes(task.status)) value = "100%";
  return <div role="cell" className="hidden px-[14px] tabular-nums text-muted-foreground md:block">{value}</div>;
}

function HistoryLoadingRows() {
  const { t } = useI18n();
  return (
    <div role="status" aria-label={t("loading")}>
      {Array.from({ length: 5 }, (_, index) => (
        <div key={index} className={cn("grid h-[76px] animate-pulse items-center border-t first:border-t-0 md:h-[62px]", TASK_LIST_COLUMNS)}>
          <Skeleton className="mx-[14px] w-40" />
          <Skeleton className="mx-[14px] hidden w-20 md:block" />
          <Skeleton className="mx-auto w-16" />
          <Skeleton className="mx-[14px] hidden w-14 md:block" />
          <Skeleton className="mx-[14px] hidden w-12 md:block" />
          <Skeleton className="mx-[14px] hidden w-24 md:block" />
          <Skeleton className="mx-[14px] hidden w-16 md:block" />
          <Skeleton className="mx-auto w-12" />
        </div>
      ))}
    </div>
  );
}

function Skeleton({ className }: { className?: string }) {
  return <span className={cn("block h-3 rounded bg-muted", className)} />;
}

function HistoryTableMessage({
  title,
  description,
  actionLabel,
  actionTo,
  onAction,
}: {
  title: string;
  description: string;
  actionLabel: string;
  actionTo?: string;
  onAction?: () => void;
}) {
  const actionClass = "mt-3 inline-flex h-8 items-center rounded-md border bg-background px-3 text-xs font-semibold text-primary outline-none hover:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring";
  return (
    <div role="row" className="flex min-h-[200px] items-center justify-center px-6 py-10 text-center">
      <div role="cell">
        <p className="text-sm font-semibold text-foreground">{title}</p>
        <p className="mt-1 max-w-md text-xs leading-5 text-muted-foreground">{description}</p>
        {actionTo ? <Link to={actionTo} className={actionClass}>{actionLabel}</Link> : <button type="button" className={actionClass} onClick={onAction}>{actionLabel}</button>}
      </div>
    </div>
  );
}
