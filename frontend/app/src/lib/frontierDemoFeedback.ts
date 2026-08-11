import type { TaskResultResponse } from "@/types";

const DISABLED_SYMPY_DIAGNOSTIC = /\s*[（(]\s*SymPy\s*(?:验证|verification)\s*[:：]\s*(?:未启用|disabled)[^）)]*[）)]/giu;

/**
 * Public Demo copy omits internal fallback diagnostics while preserving the
 * model's actual rationale. The underlying API response remains unchanged.
 */
export function hideFrontierDemoDiagnostics(comment: string | null | undefined) {
  if (!comment) return comment ?? "";
  return comment
    .replace(DISABLED_SYMPY_DIAGNOSTIC, "")
    .replace(/\n{3,}/gu, "\n\n")
    .trim();
}

export function sanitizeFrontierDemoTaskResult(
  result: TaskResultResponse | undefined,
  enabled: boolean,
) {
  if (!enabled || !result?.results) return result;
  return {
    ...result,
    results: result.results.map((student) => ({
      ...student,
      corrections: student.corrections.map((correction) => ({
        ...correction,
        comment: hideFrontierDemoDiagnostics(correction.comment),
        expert_results: correction.expert_results.map((expert) => ({
          ...expert,
          comment: hideFrontierDemoDiagnostics(expert.comment),
        })),
      })),
    })),
  };
}
