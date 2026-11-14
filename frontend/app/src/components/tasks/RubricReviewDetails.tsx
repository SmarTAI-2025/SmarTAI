import type { PreparationIssue } from "@/types";
import { MarkdownMath } from "@/components/ui/MarkdownMath";

type Field = { path: string; value: unknown };
const labels: Record<string, [string, string]> = {
  criterion: ["评分要求", "Criterion"], total_pts: ["满分", "Maximum"],
  follow_through_pts: ["沿用前一步错值时得分", "Credit for following an earlier error"],
  follow_through_keep: ["沿用前一步错值时保留比例", "Follow-through credit rate"],
  deduct_pts: ["每次扣分", "Deduction per occurrence"], max_deduction: ["累计扣分上限", "Maximum total deduction"],
  limit: ["最多计次", "Occurrence limit"], count_by: ["计次方式", "Count by"],
  required_conditions: ["给分条件", "Award conditions"], source_quote: ["教师原文", "Teacher wording"],
  answer_refs: ["对应标答内容", "Related reference material"], exclusive_group: ["互斥扣分组", "Exclusive deduction group"],
  nonblank_floor: ["非空作答最低分", "Nonblank answer minimum"], alternative: ["其他正确解法", "Alternative methods"],
};
const objectLabels: Record<string, [string, string]> = {
  maximum: ["最高分", "Maximum"], minimum: ["最低分", "Minimum"],
  deduct_per_error: ["每个错误扣分", "Deduction per error"], max_deduction: labels.max_deduction,
  source_quote: labels.source_quote,
};
const counts: Record<string, [string, string]> = {
  independent_error: ["独立错误", "Independent error"], wrong_step: ["错误步骤", "Incorrect step"],
  target_item: ["作答目标", "Target item"],
};
function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

export function rubricReviewRows(issues: PreparationIssue[] = [], locale: string) {
  const lang = locale === "zh-CN" ? 0 : 1;
  const points = (n: unknown) => typeof n === "number" && Number.isFinite(n) ? `${n} ${lang === 0 ? "分" : "pts"}` : "";
  const amount = (v: unknown, base: string) => {
    const a = object(v);
    if (!a) return "";
    if (a.points != null || a.pts != null) return points(a.points ?? a.pts);
    const fraction = typeof a.fraction === "number" ? `${Math.round(a.fraction * 10000) / 100}%` : a.fraction;
    return fraction != null ? (lang === 0 ? `${base}的 ${fraction}` : `${fraction} of ${base}`) : "";
  };
  return issues.filter(issue => issue.status === "open" && issue.field === "rubric").flatMap(issue => {
    const draft = object(issue.details?.rubric_review_draft);
    if (!draft || !Array.isArray(draft.fields)) return [];
    const fields = draft.fields.filter((f): f is Field => Boolean(object(f) && typeof f.path === "string"));
    const values = new Map(fields.map(f => [f.path, f.value]));
    const segments = values.get("part_rubric.answer_segments");
    const refs = new Map<string, string>();
    if (Array.isArray(segments)) for (const segment of segments) {
      const s = object(segment);
      if (typeof s?.id === "string" && typeof s.text === "string") refs.set(s.id, s.text);
    }
    const groups = new Map<string, number>();
    const rows: Array<{ key: string; label: string; value: string }> = [];
    const describe = (path: string, value: unknown, pending = false) => {
      const match = /^part_rubric(?:\.parts\[(\d+)\])?(?:\.(steps|deductions)\[(\d+)\])?\.([a-z_]+)$/.exec(path);
      if (!match || !labels[match[4]]) return;
      const [, part, kind, index, key] = match;
      const partLabel = values.get(`part_rubric.parts[${part}].label`);
      const scope = part === undefined ? (lang === 0 ? "整题" : "Question")
        : typeof partLabel === "string" && partLabel ? partLabel : (lang === 0 ? `小问 ${Number(part) + 1}` : `Part ${Number(part) + 1}`);
      const item = index === undefined ? "" : `${kind === "steps" ? (lang === 0 ? "步骤" : "Step") : (lang === 0 ? "扣分项" : "Deduction")} ${Number(index) + 1}`;
      let text = "";
      if (pending) text = lang === 0 ? "待核对" : "Review needed";
      else if (["total_pts", "follow_through_pts", "deduct_pts", "max_deduction"].includes(key)) text = points(value);
      else if (key === "follow_through_keep" && typeof value === "number") text = `${Math.round(value * 10000) / 100}%`;
      else if (key === "nonblank_floor") text = amount(value, lang === 0 ? "本题满分" : "the question maximum");
      else if (key === "alternative") {
        const a = object(value);
        if (a) text = Object.entries(a).flatMap(([name, v]) => {
          if (!objectLabels[name] || v == null || v === "") return [];
          const rendered = name === "maximum" || name === "minimum" ? amount(v, lang === 0 ? "小问满分" : "the part maximum")
            : name === "source_quote" ? String(v) : points(v);
          return rendered ? [`${objectLabels[name][lang]}：${rendered}`] : [];
        }).join("\n");
      } else if (key === "count_by" && typeof value === "string") text = counts[value]?.[lang] ?? "";
      else if (key === "answer_refs" && Array.isArray(value)) text = value.flatMap(v => refs.has(v) ? [refs.get(v)!] : []).join("\n");
      else if (key === "exclusive_group" && typeof value === "string") {
        if (!groups.has(value)) groups.set(value, groups.size + 1);
        text = lang === 0 ? `第 ${groups.get(value)} 组（同一目标只取一档）` : `Group ${groups.get(value)} (one loss per target)`;
      } else if (Array.isArray(value)) text = value.filter(v => typeof v === "string").join("\n");
      else if (typeof value === "string" || typeof value === "number") text = String(value);
      if (text) rows.push({ key: `${issue.issue_id}:${path}`, label: [scope, item, labels[key][lang]].filter(Boolean).join(" · "), value: text });
    };
    fields.forEach(f => describe(f.path, f.value));
    if (Array.isArray(draft.invalid_fields)) for (const path of draft.invalid_fields) {
      if (typeof path === "string") describe(path, null, true);
    }
    return rows;
  });
}

export function RubricReviewDetails({ issues, locale }: { issues?: PreparationIssue[]; locale: string }) {
  const rows = rubricReviewRows(issues, locale);
  if (!rows.length) return null;
  const zh = locale === "zh-CN";
  return <aside className="mt-4 rounded-md border border-red-200 bg-red-50/40 p-3 dark:bg-red-950/15" aria-label={zh ? "待核对的评分项" : "Scoring details to review"}>
    <h4 className="text-sm font-semibold">{zh ? "待核对的评分项" : "Scoring details to review"}</h4>
    <p className="mt-1 text-xs text-muted-foreground">{zh ? "对照下列提取结果修改评分标准，保存后再确认。" : "Use these extracted details to update the rubric, then save and confirm."}</p>
    <dl className="mt-3 max-h-80 space-y-3 overflow-y-auto text-sm">
      {rows.map(row => <div key={row.key}><dt className="text-xs font-medium text-muted-foreground">{row.label}</dt><dd className="mt-1 break-words"><MarkdownMath>{row.value}</MarkdownMath></dd></div>)}
    </dl>
  </aside>;
}
