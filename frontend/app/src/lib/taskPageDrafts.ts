import type { DraftCodec } from "./pageDraftStore";
import type { PreparationSourceRole, ProblemLibraryMaterial, ProblemSourceMode, ProblemSourceScope, ProblemStructureMode, QuestionScorePolicyInput, SubmissionIdentityMode } from "@/types";

export type SourceDraft = {
  id: string; role: PreparationSourceRole; sourceMode: ProblemSourceMode;
  file: File | null; fileName: string; libraryScope: ProblemSourceScope;
  librarySearch: string; libraryMaterial: ProblemLibraryMaterial | null;
  inlineText: string; structureMode: ProblemStructureMode; extractionHint: string;
  recognitionPages: string; recognitionTargets: string; enableMaterialOcr: boolean;
  saveToLibrary: boolean; storedFileId: string | null;
  prepared: { operationId: string; signature: string } | null;
};
export type ScorePolicyDraft = { mode: QuestionScorePolicyInput["mode"]; uniformMaxScore: string; perQuestionText: string };
export type ProblemDraft = { activeRole: PreparationSourceRole; sources: SourceDraft[]; scorePolicy: ScorePolicyDraft; recognitionProviderId: string; formError: string | null };
export type SubmissionDraft = {
  selectedFile: File | null; rosterFile: File | null; selectedFileName: string; rosterFileName: string;
  identityMode: SubmissionIdentityMode; recognitionProviderId: string;
};
export type MetadataDraft = {
  name: string; semesterId: string; courseId: string | null; courseDraft: string; tagIds: string[]; tagDraft: string;
  idempotency: { signature: string; key: string } | null;
};

const roles = ["problem", "reference_answer", "rubric", "programming_tests"] as const;
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_draft");
  return value as Record<string, unknown>;
}
function string(value: unknown, max = 12_000): string {
  if (typeof value !== "string" || value.length > max) throw new Error("invalid_draft");
  return value;
}
function choice<T extends string>(value: unknown, values: readonly T[]): T {
  if (!values.includes(value as T)) throw new Error("invalid_draft");
  return value as T;
}
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("invalid_draft");
  return value;
}
function nullableString(value: unknown): string | null { return value === null ? null : string(value, 512); }
function list<T>(value: unknown, parse: (value: unknown) => T, max = 20): T[] {
  if (!Array.isArray(value) || value.length > max) throw new Error("invalid_draft");
  return value.map(parse);
}
function pair(value: unknown, first: string, second: string) {
  const data = record(value);
  return [string(data[first]), string(data[second])] as const;
}
function codec<T>(encode: (value: T) => unknown, parse: (value: unknown) => T): DraftCodec<T> {
  return { encode, decode(value) { try { return parse(value); } catch { return null; } } };
}

export function createSourceDraft(role: PreparationSourceRole): SourceDraft {
  return { id: globalThis.crypto?.randomUUID?.() ?? `source-${Date.now()}-${Math.random().toString(16).slice(2)}`, role, sourceMode: "upload", file: null, fileName: "", libraryScope: "course", librarySearch: "", libraryMaterial: null,
    inlineText: "", structureMode: "organized", extractionHint: "", recognitionPages: "", recognitionTargets: "", enableMaterialOcr: false,
    saveToLibrary: false, storedFileId: null, prepared: null };
}
export function initialProblemDraft(): ProblemDraft {
  return { activeRole: "problem", sources: [createSourceDraft("problem")], scorePolicy: { mode: "default_10", uniformMaxScore: "10", perQuestionText: "" }, recognitionProviderId: "", formError: null };
}
export function initialSubmissionDraft(): SubmissionDraft {
  return { selectedFile: null, rosterFile: null, selectedFileName: "", rosterFileName: "", identityMode: "filename", recognitionProviderId: "" };
}

function material(value: unknown): ProblemLibraryMaterial | null {
  if (value === null) return null;
  const data = record(value);
  return { material_id: string(data.material_id, 128), filename: string(data.filename, 512) };
}
function source(value: unknown): SourceDraft {
  const data = record(value);
  const prepared = data.prepared === null ? null : pair(data.prepared, "operationId", "signature");
  return {
    id: string(data.id, 128), role: choice(data.role, roles), sourceMode: choice(data.sourceMode, ["upload", "library", "inline_text"]),
    file: null, fileName: string(data.fileName, 512), libraryScope: choice(data.libraryScope, ["course", "all"]),
    librarySearch: string(data.librarySearch, 500), libraryMaterial: material(data.libraryMaterial), inlineText: string(data.inlineText),
    structureMode: choice(data.structureMode, ["organized", "extract_from_source"]), extractionHint: string(data.extractionHint, 2000),
    recognitionPages: string(data.recognitionPages, 160), recognitionTargets: string(data.recognitionTargets, 600),
    enableMaterialOcr: boolean(data.enableMaterialOcr), saveToLibrary: boolean(data.saveToLibrary), storedFileId: nullableString(data.storedFileId),
    prepared: prepared ? { operationId: prepared[0], signature: prepared[1] } : null,
  };
}
// Run the explicit parser on writes too: only selected metadata enters the codec; explicit storage handles file bytes separately.
export const problemDraftCodec = codec<ProblemDraft>(
  (value) => parseProblem({ ...value, sources: value.sources.map((item) => ({ ...item, fileName: item.file?.name ?? item.fileName })) }),
  parseProblem,
);
function parseProblem(value: unknown): ProblemDraft {
  const data = record(value); const score = record(data.scorePolicy);
  return { activeRole: choice(data.activeRole, roles), sources: list(data.sources, source), recognitionProviderId: string(data.recognitionProviderId, 240), formError: nullableString(data.formError),
    scorePolicy: { mode: choice(score.mode, ["default_10", "uniform", "per_question"]), uniformMaxScore: string(score.uniformMaxScore, 32), perQuestionText: string(score.perQuestionText) } };
}
export const submissionDraftCodec = codec<SubmissionDraft>(
  (value) => parseSubmission({ ...value, selectedFileName: value.selectedFile?.name ?? value.selectedFileName, rosterFileName: value.rosterFile?.name ?? value.rosterFileName }),
  parseSubmission,
);
function parseSubmission(value: unknown): SubmissionDraft {
  const data = record(value);
  return { selectedFile: null, rosterFile: null, selectedFileName: string(data.selectedFileName, 512), rosterFileName: string(data.rosterFileName, 512),
    identityMode: choice(data.identityMode, ["filename", "roster", "manual_review"]), recognitionProviderId: string(data.recognitionProviderId, 240) };
}
export const metadataDraftCodec = codec<MetadataDraft>((value) => parseMetadata(value), parseMetadata);
function parseMetadata(value: unknown): MetadataDraft {
  const data = record(value); const idempotency = data.idempotency === null ? null : pair(data.idempotency, "signature", "key");
  return { name: string(data.name, 200), semesterId: string(data.semesterId, 64), courseId: nullableString(data.courseId), courseDraft: string(data.courseDraft, 300),
    tagIds: list(data.tagIds, (item) => string(item, 128), 30), tagDraft: string(data.tagDraft, 300),
    idempotency: idempotency ? { signature: idempotency[0], key: idempotency[1] } : null };
}

export function sourceSignature(source: SourceDraft, providerId: string) {
  // Pure signature: changing a setting invalidates only its prepared source, never uploads on restore.
  return JSON.stringify([source.role, source.sourceMode, source.storedFileId, source.libraryMaterial?.material_id,
    source.inlineText, source.structureMode, source.extractionHint, source.recognitionPages, source.recognitionTargets,
    source.enableMaterialOcr, source.saveToLibrary, providerId]);
}
