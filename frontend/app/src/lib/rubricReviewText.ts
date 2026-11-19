import type { PreparationIssue } from "@/types";

type Field = { path: string; value: unknown };
const labels: Record<string, [string, string]> = {
  parts: ["小问", "Parts"], steps: ["评分步骤", "Scoring steps"], deductions: ["扣分项", "Deductions"],
  criterion: ["评分要求", "Criterion"], total_pts: ["满分", "Maximum"],
  follow_through_pts: ["沿用前一步错值时得分", "Credit for following an earlier error"],
  follow_through_keep: ["沿用前一步错值时保留比例", "Follow-through credit rate"],
  deduct_pts: ["每次扣分", "Deduction per occurrence"], max_deduction: ["累计扣分上限", "Maximum total deduction"],
  limit: ["最多计次", "Occurrence limit"], count_by: ["计次方式", "Count by"],
  required_conditions: ["给分条件", "Award conditions"], source_quote: ["教师原文", "Teacher wording"],
  answer_refs: ["对应标答内容", "Related reference material"], exclusive_group: ["互斥扣分组", "Exclusive deduction group"],
  nonblank_floor: ["非空作答最低分", "Nonblank answer minimum"], alternative: ["其他正确解法", "Alternative methods"],
  test_suite: ["测试计分", "Test scoring"],
};
const objectLabels: Record<string, [string, string]> = {
  maximum: ["最高分", "Maximum"], minimum: ["最低分", "Minimum"],
  deduct_per_error: ["每个独立错误的扣分额", "Deduction per independent error"], max_deduction: labels.max_deduction,
  source_quote: labels.source_quote,
};
const counts: Record<string, [string, string]> = {
  independent_error: ["独立错误", "Independent error"], wrong_step: ["错误步骤", "Incorrect step"],
  target_item: ["作答目标", "Target item"],
};
function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function rubricReviewLines(issues: PreparationIssue[] = [], locale: string) {
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
    const rows = new Map<string, { scope: string; item: string; kind: string; fields: Map<string, string> }>();
    const partIndexes = new Set(fields.flatMap(f => /^part_rubric\.parts\[(\d+)\]/.exec(f.path)?.[1] ?? []));
    const describe = (path: string, value: unknown) => {
      const match = /^part_rubric(?:\.parts\[(\d+)\])?(?:\.(steps|deductions)\[(\d+)\])?(?:\.([a-z_]+))?$/.exec(path);
      if (!match || !labels[match[4]]) return;
      const [, part, kind, index, key] = match;
      const partLabel = values.get(`part_rubric.parts[${part}].label`);
      const scope = part === undefined ? ""
        : typeof partLabel === "string" && partLabel ? partLabel
          : partIndexes.size === 1 ? "" : (lang === 0 ? `小问 ${Number(part) + 1}` : `Part ${Number(part) + 1}`);
      const item = index === undefined ? "" : `${kind === "steps" ? (lang === 0 ? "步骤" : "Step") : (lang === 0 ? "扣分项" : "Deduction")} ${Number(index) + 1}`;
      let text = "";
      if (key === "test_suite" && value === true) text = lang === 0 ? "整组测试按通过比例计分" : "Award the suite budget in proportion to passed tests";
      else if (["total_pts", "follow_through_pts", "deduct_pts", "max_deduction"].includes(key)) text = points(value);
      else if (key === "follow_through_keep" && typeof value === "number") text = `${Math.round(value * 10000) / 100}%`;
      else if (key === "nonblank_floor") text = amount(value, lang === 0 ? "本题满分" : "the question maximum");
      else if (key === "alternative") {
        const a = object(value);
        if (a) text = Object.entries(a).flatMap(([name, v]) => {
          if (!objectLabels[name] || v == null || v === "") return [];
          const rendered = name === "maximum" || name === "minimum" ? amount(v, lang === 0 ? "小问满分" : "the part maximum")
            : name === "source_quote" ? String(v) : points(v);
          return rendered ? [`${objectLabels[name][lang]} ${rendered}`] : [];
        }).join(lang === 0 ? "，" : ", ");
      } else if (key === "count_by" && typeof value === "string") text = counts[value]?.[lang] ?? "";
      else if (key === "answer_refs" && Array.isArray(value)) text = value.flatMap(v => refs.has(v) ? [refs.get(v)!] : []).join("\n");
      else if (key === "exclusive_group" && typeof value === "string") {
        if (!groups.has(value)) groups.set(value, groups.size + 1);
        text = lang === 0 ? `第 ${groups.get(value)} 组（同一目标只取一档）` : `Group ${groups.get(value)} (one loss per target)`;
      } else if (Array.isArray(value)) text = value.filter(v => typeof v === "string").join("\n");
      else if (typeof value === "string" || typeof value === "number") text = String(value);
      if (text) {
        const rowKey = path.slice(0, path.lastIndexOf("."));
        if (!rows.has(rowKey)) rows.set(rowKey, { scope, item, kind, fields: new Map() });
        rows.get(rowKey)!.fields.set(key, text);
      }
    };
    fields.forEach(f => describe(f.path, f.value));
    const lines: string[] = [];
    for (const { scope, item, kind, fields: items } of rows.values()) {
      if (!item) {
        for (const [key, text] of items) {
          if (key === "total_pts" && !scope) continue; // The editor already shows the question maximum.
          lines.push(`${scope ? `${scope} ` : ""}${labels[key][lang]} ${text}`);
        }
        continue;
      }
      const criterion = items.get("criterion");
      const score = items.get(kind === "steps" ? "total_pts" : "deduct_pts");
      const budget = kind === "deductions" && score ? `${lang === 0 ? "扣" : "Deduct "}${score}` : score;
      const qualifiers = [...items].flatMap(([key, text]) => {
        if (["criterion", "total_pts", "deduct_pts"].includes(key)) return [];
        return [key === "test_suite" ? text : `${labels[key][lang]} ${text}`];
      });
      const notes = [budget, ...qualifiers].filter(Boolean).join(lang === 0 ? "；" : "; ");
      const suffix = notes ? (lang === 0 ? `（${notes}）` : ` (${notes})`) : "";
      lines.push(`- ${[scope, criterion || item].filter(Boolean).join(" ")}${suffix}`);
    }
    return lines;
  });
}

/** Review text is editable source material, never a sealed scoring ledger. */
export function rubricReviewText(issues: PreparationIssue[] | undefined, locale: string): string {
  return rubricReviewLines(issues, locale).join("\n");
}

export function rubricReviewDetail(issue: PreparationIssue, locale: string): string {
  const zh = locale === "zh-CN";
  const draft = object(issue.details?.rubric_review_draft);
  const values = new Map(Array.isArray(draft?.fields) ? draft.fields.flatMap(f =>
    object(f) && typeof f.path === "string" ? [[f.path, f.value] as const] : []) : []);
  const findings = Array.isArray(draft?.findings) ? draft.findings.flatMap(value => {
    const finding = object(value);
    if (!finding || typeof finding.reason !== "string" || !finding.reason.trim()) return [];
    const part = typeof finding.part === "string" ? finding.part.trim() : "";
    return [part ? `${zh ? "小问" : "Part"} ${part} · ${finding.reason}` : finding.reason];
  }) : [];
  const missingParts = Array.isArray(draft?.missing_parts) ? draft.missing_parts
    .filter((value): value is string => typeof value === "string" && Boolean(value.trim())) : [];
  if (missingParts.length) findings.push(zh ? `请补全小问 ${missingParts.join("、")} 的评分标准。`
    : `Complete the rubric for ${missingParts.length === 1 ? "part" : "parts"} ${missingParts.join(", ")}.`);
  const missing = new Set(Array.isArray(draft?.missing_fields) ? draft.missing_fields : []);
  const invalid = draft?.invalid_fields ?? issue.details?.invalid_fields;
  const fields = Array.isArray(invalid) ? invalid.flatMap(path => {
    if (typeof path !== "string") return [];
    const match = /^part_rubric(?:\.parts\[(\d+)\])?(?:\.(steps|deductions)(?:\[(\d+)\])?)?(?:\.([a-z_]+))?(?:\.([a-z_]+))?$/.exec(path);
    if (!match) return [];
    const [, part, kind, index, key, nested] = match;
    const label = values.get(`part_rubric.parts[${part}].label`);
    const partScope = part === undefined ? "" : typeof label === "string" && label ? label : String(Number(part) + 1);
    const field = key === "total_pts" && kind === "steps" ? ["分值", "Points"] : nested ? objectLabels[nested] : labels[key || kind];
    if (!field && !partScope) return [];
    const scope = [partScope && `${zh ? "小问" : "Part"} ${partScope}`,
      index !== undefined && `${kind === "steps" ? (zh ? "评分项" : "Scoring item") : (zh ? "扣分项" : "Deduction")} ${Number(index) + 1}`,
      nested && labels[key]?.[zh ? 0 : 1], field?.[zh ? 0 : 1]].filter(Boolean).join(" · ");
    return [missing.has(path) ? (zh ? `请补填${scope}。` : `Enter ${scope}.`)
      : (zh ? `请核对${scope}。` : `Check ${scope}.`)];
  }) : [];
  return [...new Set([...findings, ...fields])].join(" ") || (zh
    ? "评分标准未通过校验，请核对下方草稿。" : "The rubric could not be validated. Review the draft below.");
}
