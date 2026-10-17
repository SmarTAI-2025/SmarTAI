export const LOW_CONFIDENCE_THRESHOLD = 0.60;
export const REQUIRED_REVIEW_CONFIDENCE_THRESHOLD = 0.25;

/** Unknown or invalid confidence is not a fabricated zero or percentage. */
export function confidenceValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
}

export function confidenceNeedsAttention(value: unknown): boolean {
  const confidence = confidenceValue(value);
  return confidence !== null && confidence < LOW_CONFIDENCE_THRESHOLD;
}

export function confidenceRequiresReview(value: unknown): boolean {
  const confidence = confidenceValue(value);
  return confidence !== null && confidence < REQUIRED_REVIEW_CONFIDENCE_THRESHOLD;
}
