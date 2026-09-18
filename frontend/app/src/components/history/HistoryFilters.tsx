import { Search, X } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { AskQueryBar } from "@/components/tasks/AskQueryBar";
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
  onCancelInterpret: () => void;
  onRemoveCondition: (field: string) => void;
  onClearSmart: () => void;
  onClear: () => void;
}

export function HistoryFilters({
  query,
  courses,
  tags,
  interpretation,
  smartError,
  isInterpreting,
  onChange,
  onInterpret,
  onCancelInterpret,
  onRemoveCondition,
  onClearSmart,
  onClear,
}: HistoryFiltersProps) {
  const { locale, t } = useI18n();
  const [keywordDraft, setKeywordDraft] = useState(query.q ?? "");
  const [smartDraft, setSmartDraft] = useState("");

  useEffect(() => setKeywordDraft(query.q ?? ""), [query.q]);

  function submitKeyword(event: FormEvent) {
    event.preventDefault();
    onChange({ q: keywordDraft.trim() || undefined });
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
        <AskQueryBar locale={locale} value={smartDraft} pending={isInterpreting}
          label={t("historySmartLabel")} placeholder={t("historySmartPlaceholder")}
          onChange={(value) => { setSmartDraft(value); if (!value.trim()) onClearSmart(); }}
          onCancel={onCancelInterpret} onApply={onInterpret} />
      </div>

      <div className="mt-3 flex items-center gap-2 overflow-x-auto pb-1" role="group" aria-label={t("historyFilterRegion")}>
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
          {interpretation.explanation ? <p role="status" className="mt-1 text-xs leading-5 text-muted-foreground">{interpretation.explanation}</p> : null}
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
