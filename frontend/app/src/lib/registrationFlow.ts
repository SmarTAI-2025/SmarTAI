import type {
  PendingRegistrationFlow,
  RegistrationRequest,
  RegistrationRequestResponse,
  RegistrationResendResponse,
  RegistrationTransport,
} from "@/types/registration";

export const REGISTRATION_FLOW_STORAGE_KEY = "smartai_registration_flow_v1";

const MAX_FLOW_AGE_MS = 24 * 60 * 60 * 1000;
const MAX_FUTURE_MS = 2 * 60 * 60 * 1000;

export function createPendingRegistrationFlow(
  request: RegistrationRequest,
  response: RegistrationRequestResponse,
  transport: RegistrationTransport,
  now = Date.now(),
): PendingRegistrationFlow {
  return {
    version: 1,
    requestId: response.request_id,
    username: request.username,
    email: request.email,
    createdAt: now,
    expiresAt: deadline(now, response.expires_in_seconds),
    resendAvailableAt: deadline(now, response.resend_after_seconds),
    transport,
  };
}

export function updatePendingRegistrationFlow(
  current: PendingRegistrationFlow,
  response: RegistrationResendResponse,
  now = Date.now(),
): PendingRegistrationFlow {
  return {
    ...current,
    requestId: response.request_id,
    expiresAt: deadline(now, response.expires_in_seconds),
    resendAvailableAt: deadline(now, response.resend_after_seconds),
  };
}

export function savePendingRegistrationFlow(flow: PendingRegistrationFlow): void {
  try {
    window.sessionStorage.setItem(REGISTRATION_FLOW_STORAGE_KEY, JSON.stringify(flow));
  } catch {
    // Navigation state still lets the current interaction continue when storage is unavailable.
  }
}

export function readPendingRegistrationFlow(now = Date.now()): PendingRegistrationFlow | null {
  try {
    const raw = window.sessionStorage.getItem(REGISTRATION_FLOW_STORAGE_KEY);
    if (!raw) return null;
    const flow = parsePendingRegistrationFlow(JSON.parse(raw));
    if (!flow || now - flow.createdAt > MAX_FLOW_AGE_MS || flow.expiresAt > now + MAX_FUTURE_MS) {
      clearPendingRegistrationFlow();
      return null;
    }
    return flow;
  } catch {
    clearPendingRegistrationFlow();
    return null;
  }
}

export function clearPendingRegistrationFlow(): void {
  try {
    window.sessionStorage.removeItem(REGISTRATION_FLOW_STORAGE_KEY);
  } catch {
    // No sensitive registration data is copied to another storage mechanism.
  }
}

function parsePendingRegistrationFlow(value: unknown): PendingRegistrationFlow | null {
  if (!value || typeof value !== "object") return null;
  const flow = value as Record<string, unknown>;
  if (
    flow.version !== 1
    || typeof flow.requestId !== "string"
    || !flow.requestId
    || typeof flow.username !== "string"
    || typeof flow.email !== "string"
    || typeof flow.createdAt !== "number"
    || typeof flow.expiresAt !== "number"
    || typeof flow.resendAvailableAt !== "number"
    || !Number.isFinite(flow.createdAt)
    || !Number.isFinite(flow.expiresAt)
    || !Number.isFinite(flow.resendAvailableAt)
    || flow.requestId.length > 512
    || flow.username.length > 64
    || flow.email.length > 254
    || (flow.transport !== "api" && flow.transport !== "temporary_adapter")
  ) {
    return null;
  }
  return flow as unknown as PendingRegistrationFlow;
}

function deadline(now: number, seconds: number): number {
  if (!Number.isFinite(seconds)) return now;
  return now + Math.min(2 * 60 * 60, Math.max(0, seconds)) * 1000;
}
