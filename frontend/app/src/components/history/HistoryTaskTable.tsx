import { MoreHorizontal, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTaskProgress } from "@/hooks/useTaskProgress";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import { getTaskDestination, isTaskProcessing } from "@/lib/taskFlow";
import type { HistoryCourseFacet, TaskHistoryQuery, TaskLite, TaskTag } from "@/types";
import { HistoryTagPopover } from "./HistoryTagPopover";
import {
  formatHistoryTime,
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
  isDeleting: boolean;
  deletingTaskId: string | null;
  hasFilters: boolean;
  onFilter: (patch: Partial<TaskHistoryQuery>) => void;
  onDelete: (task: TaskLite) => void;
  onRetry: () => void;
  onClear: () => void;
}

const COLUMNS = "grid-cols-[minmax(0,1fr)_140px] md:grid-cols-[minmax(280px,1fr)_215px_120px_120px_160px_185px]";

export function HistoryTaskTable({
  tasks,
  courses,
  tags,
  isLoading,
  errorMessage,
  isDeleting,
  deletingTaskId,
  hasFilters,
  onFilter,
  onDelete,
  onRetry,
  onClear,
}: HistoryTaskTableProps) {
  const { t } = useI18n();
  return (
    <section aria-label={t("historyTableRegion")} className="w-full">
      <div className="overflow-visible pb-2 md:overflow-x-auto">
        <div role="table" aria-label={t("historyTableRegion")} aria-busy={isLoading} className="min-w-0 text-left md:min-w-[1080px]">
          <div role="row" className={cn("grid h-[42px] items-center text-[13px] font-semibold leading-4 text-muted-foreground", COLUMNS)}>
            <div role="columnheader" className="px-[14px]">{t("historyColumnTask")}</div>
            <div role="columnheader" className="px-[14px]">{t("historyColumnStage")}</div>
            <div role="columnheader" className="hidden px-[14px] md:block">{t("historyColumnProgress")}</div>
            <div role="columnheader" className="hidden px-[14px] md:block">{t("historyColumnEta")}</div>
            <div role="columnheader" className="hidden px-[14px] md:block">{t("historyColumnUpdated")}</div>
            <div role="columnheader" className="hidden px-[14px] md:block">{t("historyColumnNext")}</div>
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
                deleting={isDeleting && deletingTaskId === task.task_id}
                deleteDisabled={isDeleting}
                onFilter={onFilter}
                onDelete={() => onDelete(task)}
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
  deleting,
  deleteDisabled,
  onFilter,
  onDelete,
}: {
  task: TaskLite;
  courses: HistoryCourseFacet[];
  tags: TaskTag[];
  deleting: boolean;
  deleteDisabled: boolean;
  onFilter: (patch: Partial<TaskHistoryQuery>) => void;
  onDelete: () => void;
}) {
  const { locale, t } = useI18n();
  const course = courses.find((item) => item.id === task.course_id);
  const tagsById = useMemo(() => new Map(tags.map((tag) => [tag.id, tag])), [tags]);
  const taskTags = (task.tag_ids ?? []).map((tagId) => tagsById.get(tagId)).filter((tag): tag is TaskTag => Boolean(tag));
  const destination = getTaskDestination(task);
  const mobileMetadata = [
    task.semester_id ? formatSemesterLabel(task.semester_id, t) : null,
    course?.name ?? null,
    ...taskTags.map((tag) => tag.name),
  ].filter((value): value is string => Boolean(value));

  return (
    <div role="row" className={cn("grid min-h-[76px] items-center border-t text-[13px] leading-4 first:border-t-0 hover:bg-slate-50/70 md:min-h-[62px] dark:hover:bg-slate-800/30", COLUMNS)}>
      <div role="cell" className="min-w-0 px-[14px] py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Link to={destination} className="min-w-0 truncate font-semibold text-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring" title={task.name}>
            {task.name}
          </Link>
          {task.needs_attention ? <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-rose-500" title={t("historyNeedsAttention")} /> : null}
        </div>
        <div className="mt-1 hidden min-w-0 items-center gap-1 overflow-visible whitespace-nowrap md:flex">
          {task.semester_id ? (
            <button type="button" title={formatSemesterLabel(task.semester_id, t)} onClick={() => onFilter({ semester_id: task.semester_id ?? undefined })} className="max-w-[132px] truncate rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-600 outline-none hover:border-primary/30 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300">
              {formatSemesterLabel(task.semester_id, t)}
            </button>
          ) : null}
          {course ? (
            <button type="button" title={course.name} onClick={() => onFilter({ course_id: course.id })} className="max-w-[100px] truncate rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] text-blue-700 outline-none hover:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring dark:border-blue-800 dark:bg-blue-950/50 dark:text-blue-200">
              {course.name}
            </button>
          ) : null}
          {taskTags.slice(0, 2).map((tag) => (
            <button key={tag.id} type="button" title={tag.name} onClick={() => onFilter({ tag_ids: [tag.id] })} className={cn("max-w-[82px] truncate rounded-full border px-2 py-0.5 text-[11px] outline-none focus-visible:ring-2 focus-visible:ring-ring", TAG_TONE_CLASSES[tag.color])}>
              {tag.name}
            </button>
          ))}
          {taskTags.length > 2 ? <span className="text-[11px] text-muted-foreground">+{taskTags.length - 2}</span> : null}
          {!task.semester_id && !course && taskTags.length === 0 ? <span className="truncate text-[11px] text-muted-foreground">{t("historyMetadataUnset")}</span> : null}
          <HistoryTagPopover task={task} tags={tags} />
        </div>
        <div className="mt-1 flex min-w-0 items-center gap-1 md:hidden">
          <HistoryTagPopover task={task} tags={tags} />
          <span title={mobileMetadata.join(" · ")} className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">
            {mobileMetadata.length ? mobileMetadata.join(" · ") : t("historyMetadataUnset")}
          </span>
        </div>
        <p className="mt-1 text-[11px] text-muted-foreground md:hidden">{formatHistoryTime(task.updated_at, locale)}</p>
      </div>

      <div role="cell" className="min-w-0 px-2 py-2 md:px-[14px]">
        <button type="button" title={t(HISTORY_STAGE_KEYS[task.status])} onClick={() => onFilter({ statuses: [task.status] })} className={cn("inline-flex h-7 max-w-[185px] items-center rounded-full px-3 text-[13px] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring", historyStatusTone(task.status))}>
          <span className="truncate">{t(HISTORY_STAGE_KEYS[task.status])}</span>
        </button>
        <div className="mt-1 flex min-w-0 items-center gap-1 md:hidden">
          <Link to={destination} className="min-w-0 flex-1 truncate text-[11px] font-medium text-muted-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring">
            {t(HISTORY_ACTION_KEYS[task.status])}
          </Link>
          <HistoryTaskActions destination={destination} deleting={deleting} deleteDisabled={deleteDisabled} onDelete={onDelete} />
        </div>
      </div>

      <HistoryProgressCell task={task} />
      <div role="cell" className="hidden px-[14px] tabular-nums text-muted-foreground md:block">—</div>
      <div role="cell" className="hidden px-[14px] tabular-nums text-muted-foreground md:block">{formatHistoryTime(task.updated_at, locale)}</div>
      <div role="cell" className="hidden min-w-0 items-center gap-1 px-[14px] md:flex">
          <Link to={destination} title={t(HISTORY_ACTION_KEYS[task.status])} className="min-w-0 flex-1 truncate font-medium text-muted-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring">
          {t(HISTORY_ACTION_KEYS[task.status])}
        </Link>
        <HistoryTaskActions
          destination={destination}
          deleting={deleting}
          deleteDisabled={deleteDisabled}
          onDelete={onDelete}
        />
      </div>
    </div>
  );
}

function HistoryProgressCell({ task }: { task: TaskLite }) {
  const { t } = useI18n();
  const active = isTaskProcessing(task.status);
  const progress = useTaskProgress(task.task_id, { enabled: active });
  let value = "—";
  if (active && progress.progress) value = `${progress.percent}%`;
  if (task.status === "draft") value = t("historyProgressPending");
  if (["graded", "review_confirmed", "finalized"].includes(task.status)) value = "100%";
  return <div role="cell" className="hidden px-[14px] tabular-nums text-muted-foreground md:block">{value}</div>;
}

function HistoryTaskActions({
  destination,
  deleting,
  deleteDisabled,
  onDelete,
}: {
  destination: string;
  deleting: boolean;
  deleteDisabled: boolean;
  onDelete: () => void;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function close(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button type="button" aria-label={t("historyMoreActions")} aria-expanded={open} onClick={() => setOpen((value) => !value)} className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
        <MoreHorizontal aria-hidden="true" className="h-4 w-4" />
      </button>
      {open ? (
        <div role="menu" className="absolute right-0 top-8 z-30 w-40 rounded-lg border bg-card p-1.5 shadow-xl">
          <Link to={destination} role="menuitem" className="flex rounded-md px-2.5 py-2 text-sm text-foreground outline-none hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring">{t("historyOpenTask")}</Link>
          <button type="button" role="menuitem" disabled={deleteDisabled} onClick={onDelete} className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-sm text-danger outline-none hover:bg-rose-50 focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 dark:hover:bg-rose-950/40">
            <Trash2 aria-hidden="true" className="h-4 w-4" />
            {deleting ? t("historyDeletePending") : t("historyDelete")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

function HistoryLoadingRows() {
  const { t } = useI18n();
  return (
    <div role="status" aria-label={t("loading")}>
      {Array.from({ length: 5 }, (_, index) => (
        <div key={index} className={cn("grid h-[76px] animate-pulse items-center border-t first:border-t-0 md:h-[62px]", COLUMNS)}>
          <Skeleton className="mx-[14px] w-40" />
          <Skeleton className="mx-[14px] w-28" />
          <Skeleton className="mx-[14px] hidden w-14 md:block" />
          <Skeleton className="mx-[14px] hidden w-12 md:block" />
          <Skeleton className="mx-[14px] hidden w-24 md:block" />
          <Skeleton className="mx-[14px] hidden w-24 md:block" />
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
