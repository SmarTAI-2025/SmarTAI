import { useEffect } from "react";
import { expireSession, getAuthToken, refreshAuthToken, SESSION_ACTIVITY_KEY, SESSION_RENEWED_ACTIVITY_KEY } from "@/api/client";
import { isSessionExpired } from "@/lib/sessionExpiry";

export const SESSION_IDLE_MS = 30 * 60_000;
const RENEW_INTERVAL_MS = 60_000;

/** Only human input renews the session; queries, job polling and focus do not. */
export function startSessionActivity() {
  let lastSentActivity = 0;
  let lastAttempt = 0;
  let pending = false;
  let disposed = false;
  const initial = Number(localStorage.getItem(SESSION_ACTIVITY_KEY)) || Date.now();
  if (!localStorage.getItem(SESSION_ACTIVITY_KEY)) localStorage.setItem(SESSION_ACTIVITY_KEY, String(initial));
  function lastActivity() { return Number(localStorage.getItem(SESSION_ACTIVITY_KEY)) || initial; }
  function check() {
    if (disposed || !getAuthToken() || isSessionExpired()) return;
    const now = Date.now();
    const activity = lastActivity();
    if (now - activity >= SESSION_IDLE_MS) { expireSession(); return; }
    if (pending || activity <= lastSentActivity || now - lastAttempt < RENEW_INTERVAL_MS) return;
    pending = true; lastAttempt = now;
    void refreshAuthToken(true).then(() => {
      lastSentActivity = activity;
      localStorage.setItem(SESSION_RENEWED_ACTIVITY_KEY, String(Math.max(activity, Number(localStorage.getItem(SESSION_RENEWED_ACTIVITY_KEY)) || 0)));
    }).catch(() => {
      // A confirmed 401 expires globally; network failures leave input intact.
    }).finally(() => { pending = false; });
  }
  function interacted() {
    if (!getAuthToken() || isSessionExpired()) return;
    const now = Date.now();
    // Never let the first click after sleep resurrect an already idle session.
    if (now - lastActivity() >= SESSION_IDLE_MS) { expireSession(); return; }
    if (now - lastActivity() >= 1_000) localStorage.setItem(SESSION_ACTIVITY_KEY, String(now));
    check();
  }
  const events = ["pointerdown", "pointermove", "keydown", "scroll", "touchstart"] as const;
  for (const name of events) window.addEventListener(name, interacted, { passive: true, capture: true });
  window.addEventListener("focus", check);
  document.addEventListener("visibilitychange", check);
  const timer = window.setInterval(check, 15_000);
  // A mount is not activity and must not extend a restored session.
  lastSentActivity = Number(localStorage.getItem(SESSION_RENEWED_ACTIVITY_KEY)) || initial;
  check();
  return () => {
    disposed = true; window.clearInterval(timer);
    for (const name of events) window.removeEventListener(name, interacted, true);
    window.removeEventListener("focus", check);
    document.removeEventListener("visibilitychange", check);
  };
}

export function useSessionActivity(authenticated: boolean) {
  useEffect(() => authenticated ? startSessionActivity() : undefined, [authenticated]);
}
