export const LOW_CONFIDENCE_THRESHOLD = 0.60;

/** Unknown or invalid confidence is not a fabricated zero or percentage. */
export function confidenceValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
}

export function confidenceNeedsAttention(value: unknown): boolean {
  const confidence = confidenceValue(value);
  return confidence !== null && confidence < LOW_CONFIDENCE_THRESHOLD;
}

/** Confidence alone never makes otherwise usable content a progression blocker. */
export function confidenceRequiresReview(_value: unknown): boolean {
  return false;
}
