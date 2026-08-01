import { ChevronDown, Search, Sparkles, X } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { useImeSafeQuery } from "@/hooks/useImeSafeQuery";
import { useI18n } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n/messages";
import type {
  HistoryCourseFacet,
  HistoryInterpretation,
  HistorySort,
  TaskHistoryQuery,
  TaskStatus,
  TaskTag,
} from "@/types";
import { cn } from "@/lib/cn";
import { buildSemesterOptions, formatSemesterLabel } from "@/lib/semesters";
import {
  HISTORY_STAGE_KEYS,
  HISTORY_STATUS_OPTIONS,
  TAG_TONE_CLASSES,
} from "./historyPresentation";

interface HistoryFiltersProps {
  query: TaskHistoryQuery;
  courses: HistoryCourseFacet[];
  tags: TaskTag[];
  interpretation: HistoryInterpretation | null;
  smartError: boolean;
  isInterpreting: boolean;
  onChange: (patch: Partial<TaskHistoryQuery>) => void;
  onInterpret: (query: string) => void;
  onRemoveCondition: (field: string) => void;
  onClearSmart: () => void;
  onClear: () => void;
}

const CONTROL_CLASS = "h-8 rounded-full border bg-background px-3 text-[13px] text-muted-foreground outline-none transition-colors hover:border-slate-300 focus:border-primary focus:ring-2 focus:ring-primary/15 dark:hover:border-slate-500";

export function HistoryFilters({
  query,
  courses,
  tags,
  interpretation,
  smartError,
  isInterpreting,
  onChange,
  onInterpret,
  onRemoveCondition,
  onClearSmart,
  onClear,
}: HistoryFiltersProps) {
  const { locale, t } = useI18n();
  const [keywordDraft, setKeywordDraft] = useState(query.q ?? "");
  const [smartDraft, setSmartDraft] = useState("");
  const smartSearch = useImeSafeQuery({ value: smartDraft, onCommit: setSmartDraft });
  const semesterOptions = buildSemesterOptions();

  useEffect(() => setKeywordDraft(query.q ?? ""), [query.q]);
  useEffect(() => {
    if (interpretation) setSmartDraft("");
  }, [interpretation]);

  function submitKeyword(event: FormEvent) {
    event.preventDefault();
    onChange({ q: keywordDraft.trim() || undefined });
  }

  function submitSmart() {
    const value = smartSearch.commitDraft().trim();
    if (value) onInterpret(value);
  }

  return (
    <section
      aria-label={t("historyFilterRegion")}
      className="rounded-[10px] border bg-card px-4 py-4 sm:px-6"
    >
      <div className="grid min-w-0 gap-2 lg:grid-cols-2">
        <form className="flex min-w-0 gap-2" onSubmit={submitKeyword}>
          <label className="relative min-w-0 flex-1">
            <span className="sr-only">{t("historySearchLabel")}</span>
            <Search aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={keywordDraft}
              onChange={(event) => setKeywordDraft(event.target.value)}
              className="h-9 w-full rounded-full border bg-background pl-9 pr-3 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
              placeholder={t("historySearchPlaceholder")}
            />
          </label>
          <button type="submit" className="h-9 shrink-0 rounded-full border bg-background px-5 text-[13px] font-medium text-foreground transition-colors hover:border-primary/40 hover:text-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            {t("historySearchAction")}
          </button>
        </form>
        <form className="flex min-w-0 gap-2" onSubmit={(event) => { event.preventDefault(); submitSmart(); }}>
          <label className="relative min-w-0 flex-1">
            <span className="sr-only">{t("historySmartLabel")}</span>
            <Sparkles aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <input
              value={smartSearch.draftValue}
              onBlur={smartSearch.handleBlur}
              onChange={smartSearch.handleChange}
              onCompositionEnd={smartSearch.handleCompositionEnd}
              onCompositionStart={smartSearch.handleCompositionStart}
              className="h-9 w-full rounded-full border bg-background pl-9 pr-3 text-[13px] text-foreground outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/15"
              placeholder={t("historySmartPlaceholder")}
            />
          </label>
          <button
            type="submit"
            disabled={isInterpreting || !smartSearch.draftValue.trim()}
            className="inline-flex h-9 shrink-0 items-center justify-center gap-2 rounded-full bg-primary px-5 text-[13px] font-semibold text-primary-foreground outline-none transition-colors hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Sparkles aria-hidden="true" className="h-4 w-4" />
            {isInterpreting ? t("historySmartRunning") : t("historySmartAction")}
          </button>
        </form>
      </div>

      <div className="mt-3 flex items-center gap-2 overflow-x-auto pb-1" role="group" aria-label={t("historyFilterRegion")}>
        <label className="relative shrink-0">
          <span className="sr-only">{t("historySemester")}</span>
          <select
            className={cn(CONTROL_CLASS, "w-[180px]")}
            value={query.semester_id ?? ""}
            onChange={(event) => onChange({ semester_id: event.target.value || undefined })}
          >
            <option value="">{t("historyAllSemesters")}</option>
            {semesterOptions.map((semester) => (
              <option key={semester.id} value={semester.id}>{formatSemesterLabel(semester.id, t)}</option>
            ))}
          </select>
        </label>

        <label className="relative shrink-0">
          <span className="sr-only">{t("historyCourse")}</span>
          <select
            className={cn(CONTROL_CLASS, "w-[150px]")}
            value={query.course_id ?? ""}
            onChange={(event) => onChange({ course_id: event.target.value || undefined })}
          >
            <option value="">{t("historyAllCourses")}</option>
            {courses.map((course) => (
              <option key={course.id} value={course.id}>{course.name}{course.code ? ` · ${course.code}` : ""}</option>
            ))}
          </select>
        </label>

        <MultiSelectFilter<TaskTag>
          label={t("historyTags")}
          emptyLabel={t("historyAllTags")}
          values={tags}
          selected={query.tag_ids ?? []}
          getId={(tag) => tag.id}
          getSearchText={(tag) => tag.name}
          searchPlaceholder={t("historyTagSearchPlaceholder")}
          renderValue={(tag) => <span className={cn("rounded-full border px-2 py-0.5 text-xs", TAG_TONE_CLASSES[tag.color])}>{tag.name}</span>}
          onChange={(tagIds) => onChange({ tag_ids: tagIds.length ? tagIds : undefined })}
        />

        <MultiSelectFilter<{ status: TaskStatus }>
          label={t("historyStatus")}
          emptyLabel={t("historyAllStatuses")}
          values={HISTORY_STATUS_OPTIONS}
          selected={query.statuses ?? []}
          getId={(option) => option.status}
          renderValue={(option) => t(HISTORY_STAGE_KEYS[option.status])}
          onChange={(statuses) => onChange({ statuses: statuses.length ? statuses as TaskStatus[] : undefined })}
        />

        <ToggleFilter
          checked={Boolean(query.unfinished)}
          label={t("historyUnfinished")}
          onChange={(checked) => onChange({ unfinished: checked || undefined })}
        />
        <ToggleFilter
          checked={Boolean(query.needs_attention)}
          label={t("historyNeedsAttention")}
          onChange={(checked) => onChange({ needs_attention: checked || undefined })}
        />

        <label className="relative shrink-0">
          <span className="sr-only">{t("historySort")}</span>
          <select
            className={cn(CONTROL_CLASS, "w-[150px]")}
            value={query.sort}
            onChange={(event) => onChange({ sort: event.target.value as HistorySort })}
          >
            <option value="updated_desc">{t("historySortUpdated")}</option>
            <option value="updated_asc">{t("historySortUpdatedAsc")}</option>
            <option value="created_desc">{t("historySortCreated")}</option>
            <option value="created_asc">{t("historySortCreatedAsc")}</option>
            <option value="name_asc">{t("historySortName")}</option>
            <option value="name_desc">{t("historySortNameDesc")}</option>
            <option value="attention_first">{t("historySortAttention")}</option>
            <option value="stage_asc">{t("historySortStage")}</option>
            <option value="stage_desc">{t("historySortStageDesc")}</option>
          </select>
        </label>

        <button type="button" className="h-8 shrink-0 px-2 text-[13px] font-medium text-muted-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring" onClick={onClear}>
          {t("historyClearFilters")}
        </button>
      </div>

      {interpretation ? (
        <div className="mt-3 border-t pt-3" aria-live="polite">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold text-foreground">{t("historySmartResult")}</span>
            {interpretation.conditions.map((condition, index) => (
              <button
                key={`${condition.field}-${index}`}
                type="button"
                title={t("historySmartRemoveCondition")}
                onClick={() => onRemoveCondition(condition.field)}
                className="inline-flex max-w-full items-center gap-1 rounded-full border bg-background px-2.5 py-1 text-xs text-muted-foreground outline-none hover:border-primary/40 hover:text-primary focus-visible:ring-2 focus-visible:ring-ring"
              >
                <span className="truncate">
                  {conditionLabel(condition.field, condition.label, t)}
                  {condition.value === null ? "" : `: ${formatConditionValue(condition.value, condition.field, courses, tags, t)}`}
                </span>
                <X aria-hidden="true" className="h-3 w-3 shrink-0" />
              </button>
            ))}
            <button type="button" className="text-xs font-medium text-primary outline-none focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring" onClick={onClearSmart}>
              {t("historySmartClear")}
            </button>
          </div>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            {interpretation.conditions.length
              ? locale === "zh-CN"
                ? `${t("historySmartAppliedPrefix")}${interpretation.conditions.length}${t("historySmartAppliedSuffix")}`
                : `Applied ${interpretation.conditions.length} editable ${interpretation.conditions.length === 1 ? "condition" : "conditions"}.`
              : t("historySmartNoCondition")}
          </p>
          {interpretation.ambiguities.length ? (
            <div className="mt-2 rounded-md bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
              <strong>{t("historySmartAmbiguity")}</strong>
              {interpretation.ambiguities.map((item) => (
                <p key={`${item.fragment}-${item.message}`}>
                  {locale === "zh-CN" ? item.message : t("historySmartAmbiguityDescription")}
                  {item.candidates?.length ? ` ${t("historySmartCandidates")}${formatAmbiguityCandidates(item.candidates)}` : ""}
                </p>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {smartError ? <p className="mt-3 text-xs text-amber-700 dark:text-amber-300" role="status">{t("historySmartFailure")}</p> : null}
    </section>
  );
}

function MultiSelectFilter<T>({
  label,
  emptyLabel,
  values,
  selected,
  getId,
  getSearchText,
  searchPlaceholder,
  renderValue,
  onChange,
}: {
  label: string;
  emptyLabel: string;
  values: T[];
  selected: string[];
  getId: (value: T) => string;
  getSearchText?: (value: T) => string;
  searchPlaceholder?: string;
  renderValue: (value: T) => ReactNode;
  onChange: (selected: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
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

  const labelText = selected.length ? `${label} · ${selected.length}` : emptyLabel;
  const normalizedSearch = search.trim().toLocaleLowerCase();
  const filteredValues = getSearchText && normalizedSearch
    ? values.filter((value) => getSearchText(value).toLocaleLowerCase().includes(normalizedSearch))
    : values;
  return (
    <div ref={rootRef} className="relative shrink-0">
      <button type="button" className={cn(CONTROL_CLASS, "inline-flex min-w-[140px] items-center justify-between gap-2")} aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        <span>{labelText}</span>
        <ChevronDown aria-hidden="true" className="h-3.5 w-3.5" />
      </button>
      {open ? (
        <div className="absolute left-0 top-10 z-30 max-h-72 min-w-[230px] overflow-y-auto rounded-lg border bg-card p-2 shadow-xl" role="group" aria-label={label}>
          {getSearchText ? (
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder={searchPlaceholder}
              className="mb-1 h-8 w-full rounded-md border bg-background px-2 text-xs text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
            />
          ) : null}
          {filteredValues.length ? filteredValues.map((value) => {
            const id = getId(value);
            const checked = selected.includes(id);
            return (
              <label key={id} className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-2 text-sm hover:bg-muted/60">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => onChange(checked ? selected.filter((item) => item !== id) : [...selected, id])}
                  className="h-4 w-4 rounded border-border accent-primary"
                />
                <span className="min-w-0 truncate">{renderValue(value)}</span>
              </label>
            );
          }) : <p className="px-2 py-3 text-xs text-muted-foreground">{emptyLabel}</p>}
        </div>
      ) : null}
    </div>
  );
}

function ToggleFilter({ checked, label, onChange }: { checked: boolean; label: string; onChange: (checked: boolean) => void }) {
  return (
    <label className={cn(CONTROL_CLASS, "inline-flex shrink-0 cursor-pointer items-center gap-2", checked && "border-primary/30 bg-blue-50 text-primary dark:bg-blue-950/40")}>
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} className="h-3.5 w-3.5 rounded accent-primary" />
      <span>{label}</span>
    </label>
  );
}

const CONDITION_LABEL_KEYS: Record<string, MessageKey> = {
  q: "historySearchLabel",
  query: "historySearchLabel",
  semester: "historySemester",
  semester_id: "historySemester",
  course: "historyCourse",
  course_id: "historyCourse",
  tag: "historyTags",
  tags: "historyTags",
  tag_ids: "historyTags",
  status: "historyStatus",
  statuses: "historyStatus",
  unfinished: "historyUnfinished",
  needs_attention: "historyNeedsAttention",
  sort: "historySort",
};

function conditionLabel(field: string, fallback: string, t: (key: MessageKey) => string): string {
  const key = CONDITION_LABEL_KEYS[field];
  return key ? t(key) : fallback;
}

function formatConditionValue(
  value: string | string[] | boolean | number,
  field: string,
  courses: HistoryCourseFacet[],
  tags: TaskTag[],
  t: (key: MessageKey) => string,
): string {
  const values = Array.isArray(value) ? value : [value];
  if (field === "semester" || field === "semester_id") {
    return values.map((item) => formatSemesterLabel(String(item), t)).join(", ");
  }
  if (field === "course" || field === "course_id") {
    return values.map((item) => courses.find((course) => course.id === String(item))?.name ?? String(item)).join(", ");
  }
  if (["tag", "tags", "tag_ids"].includes(field)) {
    return values.map((item) => tags.find((tag) => tag.id === String(item))?.name ?? String(item)).join(", ");
  }
  if (field === "status" || field === "statuses") {
    return values.map((item) => HISTORY_STAGE_KEYS[item as TaskStatus] ? t(HISTORY_STAGE_KEYS[item as TaskStatus]) : String(item)).join(", ");
  }
  if (field === "sort") {
    const sortKeys: Partial<Record<HistorySort, MessageKey>> = {
      updated_desc: "historySortUpdated",
      updated_asc: "historySortUpdatedAsc",
      created_desc: "historySortCreated",
      created_asc: "historySortCreatedAsc",
      name_asc: "historySortName",
      name_desc: "historySortNameDesc",
      attention_first: "historySortAttention",
      stage_asc: "historySortStage",
      stage_desc: "historySortStageDesc",
    };
    const key = sortKeys[String(value) as HistorySort];
    return key ? t(key) : String(value);
  }
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "boolean") return value ? "✓" : "—";
  return String(value);
}

function formatAmbiguityCandidates(
  candidates: Array<string | { id?: string; name?: string; label?: string }>,
): string {
  return candidates
    .map((candidate) => typeof candidate === "string"
      ? candidate
      : candidate.name ?? candidate.label ?? candidate.id ?? "")
    .filter(Boolean)
    .join(", ");
}
