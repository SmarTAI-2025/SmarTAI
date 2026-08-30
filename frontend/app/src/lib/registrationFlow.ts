import type { EmailRegistrationResponse } from "@/types";

export const PENDING_REGISTRATION_STORAGE_KEY = "smartai_registration_flow_v2";

const MAX_FLOW_AGE_MS = 24 * 60 * 60 * 1000;
const MAX_SERVER_DEADLINE_MS = 2 * 60 * 60 * 1000;
const MAX_FUTURE_CLOCK_SKEW_MS = 5 * 60 * 1000;

export interface PendingRegistrationFlow {
  version: 2;
  requestId: string;
  maskedEmail: string;
  createdAt: number;
  expiresAt: number;
  resendAvailableAt: number;
}

export function createPendingRegistrationFlow(
  email: string,
  response: EmailRegistrationResponse,
  now = Date.now(),
): PendingRegistrationFlow {
  return {
    version: 2,
    requestId: response.request_id,
    maskedEmail: maskEmail(email),
    createdAt: now,
    expiresAt: deadline(now, response.expires_in_seconds),
    resendAvailableAt: deadline(now, response.resend_after_seconds),
  };
}

export function updatePendingRegistrationFlow(
  flow: PendingRegistrationFlow,
  response: EmailRegistrationResponse,
  now = Date.now(),
): PendingRegistrationFlow {
  return {
    ...flow,
    requestId: response.request_id,
    createdAt: now,
    expiresAt: deadline(now, response.expires_in_seconds),
    resendAvailableAt: deadline(now, response.resend_after_seconds),
  };
}

export function savePendingRegistrationFlow(flow: PendingRegistrationFlow): void {
  try {
    window.sessionStorage.setItem(PENDING_REGISTRATION_STORAGE_KEY, JSON.stringify(flow));
  } catch {
    // The next page shows a recovery action if storage is unavailable.
  }
}

export function readPendingRegistrationFlow(now = Date.now()): PendingRegistrationFlow | null {
  try {
    const raw = window.sessionStorage.getItem(PENDING_REGISTRATION_STORAGE_KEY);
    if (!raw) return null;
    const parsed = parsePendingRegistrationFlow(JSON.parse(raw), now);
    if (!parsed) clearPendingRegistrationFlow();
    return parsed;
  } catch {
    clearPendingRegistrationFlow();
    return null;
  }
}

export function clearPendingRegistrationFlow(): void {
  try {
    window.sessionStorage.removeItem(PENDING_REGISTRATION_STORAGE_KEY);
  } catch {
    // No fallback storage is used for registration metadata.
  }
}

export function maskEmail(email: string): string {
  const normalized = email.trim().toLowerCase();
  const at = normalized.lastIndexOf("@");
  if (at <= 0 || at === normalized.length - 1) return "***";
  const local = normalized.slice(0, at);
  const domain = normalized.slice(at + 1);
  if (local.length === 1) return `*@${domain}`;
  if (local.length === 2) return `${local[0]}*@${domain}`;
  return `${local[0]}${"*".repeat(Math.min(6, local.length - 2))}${local.at(-1)}@${domain}`;
}

function parsePendingRegistrationFlow(value: unknown, now: number): PendingRegistrationFlow | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const flow = value as Record<string, unknown>;
  if (
    flow.version !== 2
    || typeof flow.requestId !== "string"
    || !flow.requestId.trim()
    || flow.requestId.length > 512
    || typeof flow.maskedEmail !== "string"
    || !flow.maskedEmail.trim()
    || flow.maskedEmail.length > 320
    || !isFiniteNumber(flow.createdAt)
    || !isFiniteNumber(flow.expiresAt)
    || !isFiniteNumber(flow.resendAvailableAt)
    || flow.createdAt > now + MAX_FUTURE_CLOCK_SKEW_MS
    || now - flow.createdAt > MAX_FLOW_AGE_MS
    || flow.expiresAt <= now
    || flow.expiresAt < flow.createdAt
    || flow.expiresAt > flow.createdAt + MAX_SERVER_DEADLINE_MS
    || flow.resendAvailableAt < flow.createdAt
    || flow.resendAvailableAt > flow.createdAt + MAX_SERVER_DEADLINE_MS
  ) {
    return null;
  }
  return {
    version: 2,
    requestId: flow.requestId,
    maskedEmail: maskEmail(flow.maskedEmail),
    createdAt: flow.createdAt,
    expiresAt: flow.expiresAt,
    resendAvailableAt: flow.resendAvailableAt,
  };
}

function deadline(now: number, seconds: number): number {
  return now + Math.min(MAX_SERVER_DEADLINE_MS, Math.max(0, seconds * 1000));
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}
