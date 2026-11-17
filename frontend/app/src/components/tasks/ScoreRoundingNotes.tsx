import type { StepScore } from "@/types";
import { formatEvidenceAmount, formatScore } from "./resultsModel";

/** Display-only allocation of whole-question rounding, never a scoring row. */
export function ScoreRoundingNotes({ steps = [], zh }: { steps?: StepScore[]; zh: boolean }) {
  return <>{steps.filter(step => Math.abs(step.rounding_adjustment ?? 0) > 1e-12).map((step, index) =>
    <p key={`${step.step_no}-${index}`} className="mt-2 text-[11px] text-muted-foreground" data-score-rounding>
      {zh
        ? `净分 ${formatScore(step.score)} 分包含全题累计舍入调整 ${formatEvidenceAmount(step.rounding_adjustment!, true)} 分；扣分点仅列实际扣分。`
        : `Net score ${formatScore(step.score)} includes ${formatEvidenceAmount(step.rounding_adjustment!, true)} pts of whole-question rounding; error rows show actual deductions.`}
    </p>)}</>;
}
