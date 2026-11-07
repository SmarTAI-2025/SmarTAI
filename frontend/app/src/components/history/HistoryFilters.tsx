import { GroundedAskAnswer } from "@/components/tasks/GroundedAskAnswer";
import { X } from "lucide-react";
import { SmarTAIMascot } from "@/components/brand/SmarTAIMascot";
import type { HistoryKeywordMatch } from "./historyQuery";
import { AskQueryBar } from "@/components/tasks/AskQueryBar";
import { useI18n } from "@/i18n/I18nProvider";
import type { MessageKey } from "@/i18n/messages";
import type {
  HistoryCourseFacet,
  HistoryInterpretation,
  HistorySort,
  TaskStatus,
  TaskTag,
} from "@/types";
import { formatSemesterLabel } from "@/lib/semesters";
import { HISTORY_STAGE_KEYS } from "./historyPresentation";

interface HistoryFiltersProps {
  dashboard: boolean;
  value: string;
  onEdit: (value: string) => void;
  choices: HistoryKeywordMatch[];
  onChoose: (choice: HistoryKeywordMatch) => void;
  canClear: boolean;
  onNameSearch: () => void;
  courses: HistoryCourseFacet[];
  tags: TaskTag[];
  interpretation: HistoryInterpretation | null;
  smartError: boolean;
  isInterpreting: boolean;
  onInterpret: (query: string) => void;
  onCancelInterpret: () => void;
  onRemoveCondition: (field: string) => void;
  onClear: () => void;
}

export function HistoryFilters({
  dashboard, value, onEdit, choices, onChoose, canClear, onNameSearch,
  courses,
  tags,
  interpretation,
  smartError,
  isInterpreting,
  onInterpret,
  onCancelInterpret,
  onRemoveCondition,
  onClear,
}: HistoryFiltersProps) {
  const { locale, t } = useI18n();
  const zh = locale === "zh-CN";
  return (
    <section aria-label={t("historyFilterRegion")} className={dashboard ? "my-6" : "rounded-[10px] border bg-card px-4 py-4 sm:px-6"}>
      <div className={dashboard ? "grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2 sm:gap-x-5" : ""}>
        {dashboard && <>
          <SmarTAIMascot variant={isInterpreting ? "grading" : "thinking"} size="md" className="h-14 w-20 sm:row-span-2 sm:h-20 sm:w-28" />
          <p className="w-fit rounded-2xl rounded-bl-sm bg-primary/[0.07] px-3 py-2 text-sm text-foreground">
            {zh ? "我可以按学期找作业，也能帮你找出待复核的批改。" : "I can find grading tasks by semester or show which ones need review."}
          </p>
        </>}
        <div className={dashboard ? "col-span-2 min-w-0 sm:col-span-1" : "min-w-0"}>
          <AskQueryBar locale={locale} value={value} pending={isInterpreting} mascotOutside={dashboard}
            label={t("historySmartLabel")} placeholder={`${t("historySearchPlaceholder")} · ${t("historySmartPlaceholder")}`}
            onChange={onEdit} onCancel={onCancelInterpret} onApply={onInterpret} />
        </div>
      </div>
      {choices.length > 0 && <div className="mt-3 flex flex-wrap items-center gap-2" role="group" aria-label={t("historySmartAmbiguity")}>
        <span className="text-xs text-muted-foreground">{zh ? "你想按哪一项查找？" : "Which match would you like to use?"}</span>
        {choices.map(choice => <button key={choice.field + choice.id} type="button" onClick={() => onChoose(choice)}
          className="rounded-full border px-2.5 py-1 text-xs hover:text-primary focus-visible:ring-2 focus-visible:ring-ring">
          {t(choice.field === "q" ? "historyColumnTask" : choice.field === "course_id" ? "historyCourse" : "historyTags")} · {choice.label}
        </button>)}
      </div>}
      {(!dashboard || canClear) && <div className="mt-3 flex items-center gap-2 overflow-x-auto pb-1" role="group" aria-label={t("historyFilterRegion")}>
        <button type="button" className="h-8 shrink-0 px-2 text-[13px] font-medium text-muted-foreground outline-none hover:text-primary focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring" onClick={onClear}>
          {t("historyClearFilters")}
        </button>
      </div>}
      <GroundedAskAnswer execution={interpretation?.execution} locale={locale} />
      {interpretation && !interpretation.execution ? (
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
            <button type="button" className="text-xs font-medium text-primary outline-none focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring" onClick={onClear}>
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
      {(smartError || interpretation?.execution?.recognized === false) && value.trim() && [...value.trim()].length <= 200 &&
        <button type="button" className="mt-2 rounded text-xs font-medium text-primary hover:underline focus-visible:ring-2 focus-visible:ring-ring" onClick={onNameSearch}>
          {zh ? "按任务名称查找这段文字" : "Find this text in task names"}
        </button>}
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
