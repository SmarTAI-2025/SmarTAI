import { useCallback, useEffect, useState } from "react";
import { getAPIErrorCode, normalizeAPIError } from "@/api/client";

const DEFAULT_RATE_LIMIT_SECONDS = 60;
const MAX_RATE_LIMIT_SECONDS = 2 * 60 * 60;
const KNOWN_PUBLIC_AUTH_CODES = new Set([
  "registration_email_domain_not_allowed",
  "registration_rate_limited",
  "registration_email_delivery_failed",
  "registration_unavailable",
  "verification_link_expired",
  "verification_link_already_used",
  "verification_link_invalid",
  "password_reset_rate_limited",
  "password_reset_unavailable",
  "password_reset_link_expired",
  "password_reset_link_already_used",
  "password_reset_link_invalid",
  "public_auth_response_invalid",
]);

export function useRetryCooldown() {
  const [deadline, setDeadline] = useState(0);
  const [now, setNow] = useState(Date.now());
  const seconds = Math.max(0, Math.ceil((deadline - now) / 1000));
  const active = seconds > 0;

  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);

  const start = useCallback((delaySeconds: number) => {
    const safeSeconds = Math.min(
      MAX_RATE_LIMIT_SECONDS,
      Math.max(0, Math.ceil(delaySeconds)),
    );
    const current = Date.now();
    setNow(current);
    setDeadline(current + safeSeconds * 1000);
  }, []);

  return { active, seconds, start };
}

export function rateLimitDelay(error: unknown, stableCode: string): number {
  const normalized = normalizeAPIError(error);
  const code = getAPIErrorCode(error);
  if (code && KNOWN_PUBLIC_AUTH_CODES.has(code)) {
    if (code !== stableCode) return 0;
  } else if (normalized.status !== 429) {
    return 0;
  }
  return Math.min(
    MAX_RATE_LIMIT_SECONDS,
    Math.max(0, normalized.retryAfterSeconds ?? DEFAULT_RATE_LIMIT_SECONDS),
  );
}

export function formatCooldown(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}
