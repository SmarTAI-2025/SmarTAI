import { useSyncExternalStore } from "react";

// A route must react even when React Query still holds a successful /auth/me.
let expired = false;
const listeners = new Set<() => void>();
export function setSessionExpired(value: boolean) {
  if (expired === value) return;
  expired = value;
  listeners.forEach((listener) => listener());
}
function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function useSessionExpired() {
  return useSyncExternalStore(subscribe, () => expired, () => false);
}
