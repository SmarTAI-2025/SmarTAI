import type {
  MajorQuestionStructureV1,
  RubricPointItemV1,
  RubricPointSummaryV1,
} from "@/types";

const POINT_PATTERN_SOURCE = String.raw`((?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?)\s*(?:分|points?|pts?)`;

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function normalizeLabel(value: string): string {
  return value
    .normalize("NFKC")
    .trim()
    .toLocaleLowerCase()
    .replace(/^[([]/, "")
    .replace(/[)\]]$/, "")
    .replace(/[.、:：\-\s]/g, "");
}

function markerPattern(label: string): RegExp {
  const token = escapeRegExp(normalizeLabel(label));
  const parenthesized = String.raw`[（(]\s*${token}\s*[)）]`;
  return new RegExp(
    String.raw`(?:第\s*${parenthesized}\s*问|(?:part|小问)\s*${parenthesized}|${parenthesized})`,
    "giu",
  );
}

export function parseScoreHundredths(value: string | number): number | null {
  const text = String(value).trim();
  const match = /^(0|[1-9][0-9]*)(?:\.([0-9]{1,2}))?$/.exec(text);
  if (!match) return null;
  const whole = Number(match[1]);
  const fraction = (match[2] ?? "").padEnd(2, "0");
  const hundredths = whole * 100 + Number(fraction || "0");
  return Number.isSafeInteger(hundredths) ? hundredths : null;
}

export function formatHundredths(value: number): string {
  const sign = value < 0 ? "-" : "";
  const absolute = Math.abs(value);
  const whole = Math.floor(absolute / 100);
  const fraction = String(absolute % 100).padStart(2, "0").replace(/0+$/, "");
  return `${sign}${whole}${fraction ? `.${fraction}` : ""}`;
}

interface MarkerOccurrence {
  start: number;
  end: number;
  subpartId: string;
  label: string;
}

export function summarizeRubricPoints(
  criterion: string,
  maxScore: string | number,
  structure?: MajorQuestionStructureV1 | null,
): RubricPointSummaryV1 {
  const maximum = parseScoreHundredths(maxScore);
  const majorMaxScore = maximum === null ? String(maxScore) : formatHundredths(maximum);
  const subparts = structure?.subparts ?? [];
  const occurrences: MarkerOccurrence[] = [];
  for (const part of subparts) {
    for (const match of criterion.matchAll(markerPattern(part.label))) {
      occurrences.push({
        start: match.index ?? 0,
        end: (match.index ?? 0) + match[0].length,
        subpartId: part.subpart_id,
        label: part.label,
      });
    }
  }
  occurrences.sort((left, right) => left.start - right.start || left.end - right.end);

  const allocations = new Map<string, number[]>();
  for (const part of subparts) allocations.set(part.subpart_id, []);
  const seen = new Set<string>();
  for (let index = 0; index < occurrences.length; index += 1) {
    const occurrence = occurrences[index];
    const spanKey = `${occurrence.start}:${occurrence.end}:${occurrence.subpartId}`;
    if (seen.has(spanKey)) continue;
    seen.add(spanKey);
    const segmentEnd = index + 1 < occurrences.length
      ? occurrences[index + 1].start
      : Math.min(criterion.length, occurrence.end + 240);
    const segment = criterion.slice(occurrence.end, segmentEnd);
    const componentScores: number[] = [];
    const pointPattern = new RegExp(POINT_PATTERN_SOURCE, "giu");
    for (const pointMatch of segment.matchAll(pointPattern)) {
      const pointStart = pointMatch.index ?? 0;
      const prefix = segment.slice(Math.max(0, pointStart - 20), pointStart);
      if (/(?:总分|合计|total)\s*[:：=为-]?\s*$/iu.test(prefix)) continue;
      const parsed = parseScoreHundredths(pointMatch[1]);
      if (parsed !== null) componentScores.push(parsed);
    }
    if (componentScores.length) {
      allocations.get(occurrence.subpartId)?.push(
        componentScores.reduce((total, score) => total + score, 0),
      );
    }
  }

  const hasExplicit = [...allocations.values()].some((values) => values.length > 0);
  if (!hasExplicit) {
    return {
      contract_version: 1,
      has_explicit_subpart_points: false,
      items: [],
      total_points: null,
      major_max_score: majorMaxScore,
      is_valid: maximum !== null,
      issue_code: null,
    };
  }

  const duplicate = [...allocations.values()].some((values) => values.length > 1);
  const incomplete = [...allocations.values()].some((values) => values.length === 0);
  const items: RubricPointItemV1[] = subparts.flatMap((part) => {
    const score = allocations.get(part.subpart_id)?.[0];
    return score === undefined ? [] : [{
      subpart_id: part.subpart_id,
      label: part.label,
      points: formatHundredths(score),
    }];
  });
  const total = items.reduce(
    (sum, item) => sum + (parseScoreHundredths(item.points) ?? 0),
    0,
  );
  const issueCode = duplicate
    ? "rubric_subpart_points_duplicate"
    : incomplete
      ? "rubric_subpart_points_incomplete"
      : maximum === null || total !== maximum
        ? "rubric_subpart_points_mismatch"
        : null;
  return {
    contract_version: 1,
    has_explicit_subpart_points: true,
    items,
    total_points: formatHundredths(total),
    major_max_score: majorMaxScore,
    is_valid: issueCode === null,
    issue_code: issueCode,
  };
}
