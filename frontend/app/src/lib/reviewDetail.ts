import type { QuestionSummary, StudentSummary } from "@/components/tasks/resultsModel";

export type MatchKind = "exact" | "related";

export interface ReviewSearchItem {
  id: string;
  primary: string;
  secondary: string;
  exactValues: string[];
  searchable: string[];
}

export interface ReviewSearchMatch {
  item: ReviewSearchItem;
  kind: MatchKind;
}

export function studentSearchItems(students: StudentSummary[]): ReviewSearchItem[] {
  return students.map((student) => ({
    id: student.id,
    primary: student.name,
    secondary: student.name === student.id ? student.id : student.id,
    exactValues: [student.id, student.name],
    searchable: [
      student.id,
      student.name,
      student.lowConfidenceCount ? "低置信 low confidence" : "",
      student.reviewCount ? "待复核 needs review" : "",
    ],
  }));
}

export function questionSearchItems(questions: QuestionSummary[]): ReviewSearchItem[] {
  return questions.map((question) => ({
    id: question.id,
    primary: question.label,
    secondary: question.type || compactExcerpt(question.stem),
    exactValues: [question.id, question.label, question.problem?.number ?? ""],
    searchable: [
      question.id,
      question.label,
      question.type ?? "",
      question.stem ?? "",
      semanticQuestionAliases(question),
      question.lowConfidenceCount ? "低置信 low confidence" : "",
      question.reviewCount ? "待复核 needs review" : "",
    ],
  }));
}

export function matchReviewItems(items: ReviewSearchItem[], rawQuery: string): ReviewSearchMatch[] {
  const query = normalize(rawQuery);
  if (!query) return items.map((item) => ({ item, kind: "related" }));

  const tokens = query.split(/\s+/).filter(Boolean);
  const matches: ReviewSearchMatch[] = [];
  for (const item of items) {
    const exact = item.exactValues.some((value) => normalize(value) === query);
    if (exact) {
      matches.push({ item, kind: "exact" });
      continue;
    }
    const haystack = normalize(item.searchable.filter(Boolean).join(" "));
    const compactHaystack = compact(haystack);
    const compactQuery = compact(query);
    const related = haystack.includes(query)
      || (compactQuery.length >= 2 && compactHaystack.includes(compactQuery))
      || tokens.every((token) => haystack.includes(token))
      || (compactQuery.length >= 3 && isSubsequence(compactQuery, compactHaystack));
    if (related) matches.push({ item, kind: "related" });
  }
  return matches;
}

function semanticQuestionAliases(question: QuestionSummary): string {
  const source = normalize(`${question.type ?? ""} ${question.stem ?? ""}`);
  const aliases: string[] = [];
  if (/积分|integral|integrat|微积分|\\int|∫/.test(source)) aliases.push("积分题 calculus integration");
  if (/证明|proof|prove/.test(source)) aliases.push("证明题 proof");
  if (/编程|代码|program|code/.test(source)) aliases.push("编程题 programming code");
  if (/计算|calculate|compute/.test(source)) aliases.push("计算题 calculation");
  if (/概念|concept|explain/.test(source)) aliases.push("概念题 concept");
  return aliases.join(" ");
}

function compactExcerpt(value?: string) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > 52 ? `${text.slice(0, 52)}…` : text;
}

function normalize(value: string) {
  return String(value ?? "").normalize("NFKC").trim().toLocaleLowerCase();
}

function compact(value: string) {
  return value.replace(/[\s\p{P}\p{S}]+/gu, "");
}

function isSubsequence(needle: string, haystack: string) {
  let index = 0;
  for (const character of haystack) {
    if (character === needle[index]) index += 1;
    if (index === needle.length) return true;
  }
  return false;
}
