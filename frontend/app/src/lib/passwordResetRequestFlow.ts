import type { PasswordResetRequestResponse } from "@/types";

export const PASSWORD_RESET_REQUEST_STORAGE_KEY = "smartai_password_reset_request_v1";

const MAX_MARKER_LIFETIME_MS = 2 * 60 * 60 * 1000;
const MAX_FUTURE_CLOCK_SKEW_MS = 5 * 60 * 1000;

export interface PasswordResetRequestMarker {
  version: 1;
  createdAt: number;
  expiresAt: number;
  resendAvailableAt: number;
}

export function createPasswordResetRequestMarker(
  response: PasswordResetRequestResponse,
  now = Date.now(),
): PasswordResetRequestMarker {
  return {
    version: 1,
    createdAt: now,
    expiresAt: deadline(now, response.expires_in_seconds),
    resendAvailableAt: deadline(now, response.resend_after_seconds),
  };
}

export function savePasswordResetRequestMarker(marker: PasswordResetRequestMarker): void {
  try {
    window.sessionStorage.setItem(PASSWORD_RESET_REQUEST_STORAGE_KEY, JSON.stringify(marker));
  } catch {
    // No email, password, or token is copied to a fallback store.
  }
}

export function readPasswordResetRequestMarker(
  now = Date.now(),
): PasswordResetRequestMarker | null {
  try {
    const raw = window.sessionStorage.getItem(PASSWORD_RESET_REQUEST_STORAGE_KEY);
    if (!raw) return null;
    const marker = parseMarker(JSON.parse(raw), now);
    if (!marker) clearPasswordResetRequestMarker();
    return marker;
  } catch {
    clearPasswordResetRequestMarker();
    return null;
  }
}

export function clearPasswordResetRequestMarker(): void {
  try {
    window.sessionStorage.removeItem(PASSWORD_RESET_REQUEST_STORAGE_KEY);
  } catch {
    // The marker is non-sensitive and expires server-side as well.
  }
}

function parseMarker(value: unknown, now: number): PasswordResetRequestMarker | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const marker = value as Record<string, unknown>;
  if (
    Object.keys(marker).length !== 4
    || marker.version !== 1
    || !isFiniteNumber(marker.createdAt)
    || !isFiniteNumber(marker.expiresAt)
    || !isFiniteNumber(marker.resendAvailableAt)
    || marker.createdAt > now + MAX_FUTURE_CLOCK_SKEW_MS
    || marker.expiresAt <= now
    || marker.expiresAt < marker.createdAt
    || marker.expiresAt > marker.createdAt + MAX_MARKER_LIFETIME_MS
    || marker.resendAvailableAt < marker.createdAt
    || marker.resendAvailableAt > marker.createdAt + MAX_MARKER_LIFETIME_MS
  ) return null;
  return {
    version: 1,
    createdAt: marker.createdAt,
    expiresAt: marker.expiresAt,
    resendAvailableAt: marker.resendAvailableAt,
  };
}

function deadline(now: number, seconds: number): number {
  return now + Math.min(MAX_MARKER_LIFETIME_MS, Math.max(0, seconds * 1000));
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
