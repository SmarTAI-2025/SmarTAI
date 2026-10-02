import { ArrowRight, LoaderCircle, Save } from "lucide-react";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { toast } from "sonner";
import { normalizeAPIError } from "@/api/client";
import {
  useCourses,
  useCourseSearch,
  useCreateCourse,
  useCreateTag,
  useCreateTask,
  useExperts,
  useTask,
  useTags,
  useTagSearch,
  useUpdateTask,
} from "@/api/hooks";
import { NewTaskStepper } from "@/components/new-task/NewTaskStepper";
import { SmartCatalogField } from "@/components/new-task/SmartCatalogField";
import { usePageDraft } from "@/hooks/usePageDraft";
import { PageDraftNotice } from "@/components/ui/PageDraftNotice";
import { metadataDraftCodec } from "@/lib/taskPageDrafts";
import { useI18n } from "@/i18n/I18nProvider";
import { modelDisplayName } from "@/lib/modelPresentation";
import { buildSemesterOptions, formatSemesterLabel, getCurrentSemesterId } from "@/lib/semesters";
import type { Course, TaskMetadataPatch, TaskTag } from "@/types";

export function NewTaskPage() {
  const { taskId } = useParams();
  const taskQuery = useTask(taskId, { refetchOnMount: "always" });
  if (taskId && (taskQuery.isLoading || (taskQuery.isFetching && !taskQuery.isFetchedAfterMount))) return <div role="status"><LoaderCircle className="animate-spin" /></div>;
  const scope = taskId ? `metadata:${taskId}:${taskMetadataSignature(taskQuery.data ?? {})}` : "new-task";
  return <NewTaskForm key={scope} scope={scope} taskQuery={taskQuery} />;
}

function NewTaskForm({ scope, taskQuery }: { scope: string; taskQuery: ReturnType<typeof useTask> }) {
  const { locale, t } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const { taskId } = useParams();
  const [searchParams] = useSearchParams();
  const isEditing = Boolean(taskId);
  const returnTo = safeTaskReturnPath(searchParams.get("returnTo"), taskId);
  const returnReachableStep = isEditing ? taskStepFromPath(returnTo) : 0;
  const semesterOptions = useMemo(() => buildSemesterOptions(), []);
  const initialSemester = useMemo(() => {
    const current = getCurrentSemesterId();
    return semesterOptions.some((option) => option.id === current)
      ? current
      : semesterOptions.at(-1)?.id ?? "";
  }, [semesterOptions]);
  const draft = usePageDraft(scope, () => ({
    name: taskQuery.data?.name ?? "",
    semesterId: semesterOptions.some((option) => option.id === taskQuery.data?.semester_id)
      ? taskQuery.data!.semester_id! : initialSemester,
    courseId: taskQuery.data?.course_id ?? null, courseDraft: "",
    tagIds: taskQuery.data?.tag_ids ?? [], tagDraft: "", idempotency: null as { signature: string; key: string } | null,
  }), metadataDraftCodec);
  const [name, setName] = draft.field("name");
  const [semesterId, setSemesterId] = draft.field("semesterId");
  const [courseId, setCourseId] = draft.field("courseId");
  const [courseDraft, setCourseDraft] = draft.field("courseDraft");
  const [tagIds, setTagIds] = draft.field("tagIds");
  const [tagDraft, setTagDraft] = draft.field("tagDraft");
  const [formError, setFormError] = useState<string | null>(null);
  const [editHydrated, setEditHydrated] = useState(false);
  const idempotencyRef = useRef(draft.value.idempotency);

  const debouncedCourseDraft = useDebouncedValue(courseDraft, 180);
  const debouncedTagDraft = useDebouncedValue(tagDraft, 180);
  const coursesQuery = useCourses();
  const courseSearch = useCourseSearch(debouncedCourseDraft);
  const tagsQuery = useTags();
  const tagSearch = useTagSearch(debouncedTagDraft);
  const createCourse = useCreateCourse();
  const createTag = useCreateTag();
  const createTask = useCreateTask();
  const updateTask = useUpdateTask();
  const expertsQuery = useExperts();
  const course = (coursesQuery.data ?? []).find((item) => item.id === courseId) ?? null;
  const tags = (tagsQuery.data ?? []).filter((item) => tagIds.includes(item.id));
  const setCourse = (item: Course | null) => setCourseId(item?.id ?? null);
  const setTags = (next: TaskTag[] | ((items: TaskTag[]) => TaskTag[])) => setTagIds((typeof next === "function" ? next(tags) : next).map((item) => item.id));
  const reachableStep = isEditing
    ? Math.max(returnReachableStep, (taskQuery.data?.problem_count ?? 0) > 0 ? 2 : 0)
    : 0;
  const enabledExperts = (expertsQuery.data ?? []).filter((expert) => expert.enabled);
  const courseQueryIsCurrent = debouncedCourseDraft.trim() === courseDraft.trim();
  const tagQueryIsCurrent = debouncedTagDraft.trim() === tagDraft.trim();
  const isSaving = createTask.isPending || updateTask.isPending;

  useEffect(() => {
    if (!isEditing || editHydrated || !taskQuery.data || !coursesQuery.isSuccess || !tagsQuery.isSuccess) return;
    setEditHydrated(true);
  }, [coursesQuery.isSuccess, editHydrated, isEditing, tagsQuery.isSuccess, taskQuery.data]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const trimmedName = name.trim();
    if (!trimmedName) {
      setFormError(t("newTaskNameRequired"));
      return;
    }
    if (!semesterId) {
      setFormError(t("newTaskSemesterRequired"));
      return;
    }
    if (courseDraft.trim() || tagDraft.trim()) {
      setFormError(t("newTaskUncommittedCatalog"));
      return;
    }

    const payload = {
      name: trimmedName,
      semester_id: semesterId,
      course_id: course?.id ?? null,
      tag_ids: tags.map((tag) => tag.id).sort(),
    } satisfies TaskMetadataPatch;

    if (isEditing && taskId) {
      try {
        await updateTask.mutateAsync({ taskId, patch: payload });
        draft.clear();
        toast.success(t("newTaskUpdateSuccess"));
        navigate(returnTo, { replace: true, state: location.state });
      } catch (error) {
        setFormError(normalizeAPIError(error).message);
      }
      return;
    }

    const signature = JSON.stringify(payload);
    if (!idempotencyRef.current || idempotencyRef.current.signature !== signature) {
      idempotencyRef.current = { signature, key: createIdempotencyKey() };
      draft.field("idempotency")[1](idempotencyRef.current);
    }

    try {
      const task = await createTask.mutateAsync({
        ...payload,
        idempotencyKey: idempotencyRef.current.key,
      });
      draft.clear();
      toast.success(t("newTaskCreateSuccess"));
      navigate(`/tasks/${task.task_id}/upload/problems`);
    } catch (error) {
      setFormError(normalizeAPIError(error).message);
    }
  }

  const modelSummary = expertsQuery.isLoading
    ? t("newTaskModelsLoading")
    : enabledExperts.length
      ? `${t("newTaskModelsConfiguredPrefix")}${enabledExperts.length}${t("newTaskModelsConfiguredSuffix")}${enabledExperts.slice(0, 2).map(modelDisplayName).join(locale === "zh-CN" ? "、" : ", ")}${enabledExperts.length > 2 ? t("newTaskModelsMore") : ""}`
      : t("newTaskModelsMissing");

  if (isEditing && (taskQuery.isError || coursesQuery.isError || tagsQuery.isError)) {
    return (
      <div className="w-full max-w-[1300px]">
        <TaskMetadataHeading editing />
        <NewTaskStepper currentStep={0} reachableStep={reachableStep} returnState={location.state} />
        <div role="alert" className="mx-auto mt-[45px] max-w-[900px] rounded-[8px] border bg-card px-6 py-12 text-center">
          <p className="text-base font-semibold text-foreground">{t("newTaskEditLoadError")}</p>
          <Link to={returnTo} state={location.state} replace className="mt-4 inline-flex h-9 items-center rounded-[7px] border px-4 text-sm font-semibold text-foreground hover:bg-muted">
            {t("newTaskBackToCurrentTask")}
          </Link>
        </div>
      </div>
    );
  }

  if (isEditing && !editHydrated) {
    return (
      <div className="w-full max-w-[1300px]">
        <TaskMetadataHeading editing />
        <NewTaskStepper currentStep={0} reachableStep={reachableStep} returnState={location.state} />
        <div className="mx-auto mt-[45px] flex min-h-[280px] max-w-[900px] items-center justify-center rounded-[8px] border bg-card text-sm text-muted-foreground">
          <LoaderCircle aria-hidden="true" className="mr-2 h-4 w-4 animate-spin" />
          {t("newTaskEditLoading")}
        </div>
      </div>
    );
  }

  return (
    <div className="w-full max-w-[1300px]">
      <TaskMetadataHeading editing={isEditing} />
      <NewTaskStepper currentStep={0} reachableStep={reachableStep} returnState={location.state} />

      <PageDraftNotice notice={draft.notice} disabled={isSaving} onDiscard={() => {
        draft.reset(); setFormError(null); idempotencyRef.current = null;
        if (isEditing) setEditHydrated(false);
      }} />
      <form id="new-task-form" onSubmit={handleSubmit} className="mt-[35px] min-h-[510px] max-w-full rounded-[8px] border bg-card p-5 sm:p-10 xl:ml-[200px] xl:w-[900px] xl:px-[49px] xl:pb-[39px] xl:pt-[39px]">
        <div className="grid gap-5 xl:block">
          <div>
            <label htmlFor="new-task-name" className="block text-[14px] font-semibold leading-5 text-foreground">{t("newTaskNameLabel")}</label>
            <input
              id="new-task-name"
              autoFocus
              disabled={isSaving}
              value={name}
              onChange={(event) => { setName(event.target.value); setFormError(null); }}
              placeholder={t("newTaskNamePlaceholder")}
              className="mt-1 h-11 w-full rounded-[8px] border bg-card px-3 text-[14px] text-foreground outline-none transition placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
              required
            />
          </div>

          <div className="grid gap-5 md:grid-cols-2 md:gap-10 xl:mt-[22px]">
            <div>
              <label htmlFor="new-task-semester" className="block text-[14px] font-semibold leading-5 text-foreground">{t("newTaskSemesterLabel")}</label>
              <p className="sr-only">{t("newTaskSemesterHint")}</p>
              <select id="new-task-semester" disabled={isSaving} value={semesterId} onChange={(event) => setSemesterId(event.target.value)} className="mt-1 h-11 w-full rounded-[8px] border bg-card px-3 text-[14px] text-foreground outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:cursor-wait disabled:opacity-60" required>
                {semesterOptions.map((semester) => <option key={semester.id} value={semester.id}>{formatSemesterLabel(semester.id, t)}</option>)}
              </select>
            </div>
            <SmartCatalogField<Course>
              label={t("newTaskCourseLabel")}
              hint={t("newTaskCourseHint")}
              placeholder={t("newTaskCoursePlaceholder")}
              resource="course"
              query={courseDraft}
              onQueryChange={setCourseDraft}
              selected={course ? [course] : []}
              initialItems={coursesQuery.data ?? []}
              searchCandidates={courseQueryIsCurrent ? courseSearch.data?.items ?? [] : []}
              isSearching={!courseQueryIsCurrent || courseSearch.isFetching}
              isCreating={createCourse.isPending || isSaving}
              getId={(item) => item.id}
              getLabel={(item) => item.name}
              getMeta={(item) => item.code || undefined}
              onSelect={setCourse}
              onRemove={() => setCourse(null)}
              onCreate={async (value, force) => createCourse.mutateAsync({ name: value, force_create: force })}
            />
          </div>

          <div className="xl:mt-[22px]">
            <SmartCatalogField<TaskTag>
              label={t("newTaskTagsLabel")}
              hint={t("newTaskTagsHint")}
              placeholder={t("newTaskTagsPlaceholder")}
              resource="tag"
              query={tagDraft}
              onQueryChange={setTagDraft}
              selected={tags}
              initialItems={tagsQuery.data ?? []}
              searchCandidates={tagQueryIsCurrent ? tagSearch.data?.items ?? [] : []}
              isSearching={!tagQueryIsCurrent || tagSearch.isFetching}
              isCreating={createTag.isPending || isSaving}
              multiple
              getId={(item) => item.id}
              getLabel={(item) => item.name}
              getMeta={(item) => item.usage_count
                ? locale === "zh-CN"
                  ? `${item.usage_count}${t("newTaskTagUsageSuffix")}`
                  : `${item.usage_count} ${item.usage_count === 1 ? "task uses" : "tasks use"} this tag`
                : undefined}
              onSelect={(item) => setTags((current) => current.some((tag) => tag.id === item.id) ? current : [...current, item])}
              onRemove={(item) => setTags((current) => current.filter((tag) => tag.id !== item.id))}
              onCreate={async (value, force) => createTag.mutateAsync({ name: value, color: "slate", force_create: force })}
            />
          </div>

          <div className="flex h-14 items-center justify-between gap-4 rounded-[8px] border bg-slate-50 px-4 dark:bg-slate-800/50 xl:mt-[27px]">
            <div className="min-w-0">
              <p className="text-[13px] font-semibold text-foreground">{t("newTaskModelSummaryLabel")}</p>
              <p className="mt-1 truncate text-xs text-muted-foreground">{modelSummary}</p>
            </div>
            <Link to={`/settings/byok?returnTo=${encodeURIComponent(location.pathname + location.search)}`} aria-disabled={isSaving} tabIndex={isSaving ? -1 : undefined} onClick={(event) => { if (isSaving) event.preventDefault(); }} className="shrink-0 text-xs font-semibold text-primary outline-none hover:underline focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring aria-disabled:cursor-wait aria-disabled:opacity-50">{t("newTaskManageModels")}</Link>
          </div>

          {formError ? <div role="alert" className="rounded-[6px] border border-danger/30 bg-red-50 px-3 py-2 text-xs text-danger dark:bg-red-950/30 xl:mt-[19px]">{formError}</div> : null}
        </div>
      </form>

      <div className="mt-[30px] flex max-w-full justify-end xl:ml-[200px] xl:w-[900px] xl:pr-[10px]">
        <button form="new-task-form" type="submit" disabled={isSaving} className="inline-flex h-10 w-full items-center justify-center gap-2 rounded-[8px] bg-primary px-5 text-[14px] font-semibold text-primary-foreground outline-none transition hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50 sm:w-[180px]">
          {isSaving ? <LoaderCircle aria-hidden="true" className="h-4 w-4 animate-spin" /> : null}
          {isSaving
            ? t(isEditing ? "newTaskSavingTask" : "newTaskCreatingTask")
            : t(isEditing ? "newTaskSaveAndReturn" : "newTaskCreateAndAdd")}
          {!isSaving ? (isEditing
            ? <Save aria-hidden="true" className="h-4 w-4" />
            : <ArrowRight aria-hidden="true" className="h-4 w-4" />) : null}
        </button>
      </div>


    </div>
  );
}

function TaskMetadataHeading({
  editing,
}: {
  editing: boolean;
}) {
  const { t } = useI18n();
  return (
    <div className="flex min-h-9 items-center gap-4">
      <h1 className="text-[30px] font-bold leading-9 tracking-[-0.02em] text-foreground">
        {t(editing ? "newTaskEditTitle" : "newTaskTitle")}
      </h1>
    </div>
  );
}

function taskStepFromPath(pathname: string) {
  if (pathname.includes("/results")) return 7;
  if (pathname.includes("/review")) return 6;
  if (pathname.includes("/grading/")) return 5;
  if (pathname.includes("/students/") || pathname.endsWith("/submissions")) return 4;
  if (pathname.includes("/submissions/upload") || pathname.includes("/submissions/progress") || pathname.includes("/grading-setup")) return 3;
  if (pathname.includes("/questions")) return 2;
  if (pathname.includes("/upload/problems") || pathname.includes("/problems/progress")) return 1;
  return 0;
}

function taskMetadataSignature(patch: TaskMetadataPatch): string {
  return JSON.stringify({
    name: patch.name?.trim() ?? "",
    semester_id: patch.semester_id ?? null,
    course_id: patch.course_id ?? null,
    tag_ids: [...(patch.tag_ids ?? [])].sort(),
  });
}

function safeTaskReturnPath(raw: string | null, taskId?: string): string {
  const fallback = taskId ? `/tasks/${taskId}/upload/problems` : "/tasks/new";
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return fallback;
  try {
    const parsed = new URL(raw, window.location.origin);
    const belongsToTask = Boolean(taskId && (
      parsed.pathname === `/tasks/${taskId}`
      || parsed.pathname.startsWith(`/tasks/${taskId}/`)
    ));
    if (!belongsToTask && parsed.pathname !== "/history" && parsed.pathname !== "/") return fallback;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return fallback;
  }
}

function useDebouncedValue(value: string, delay: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timeout = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timeout);
  }, [delay, value]);
  return debounced;
}

function createIdempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `task-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
