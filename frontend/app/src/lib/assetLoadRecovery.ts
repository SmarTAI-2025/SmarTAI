const RELOAD_KEY = "smartai:asset-reload-attempted";

export function isAssetLoadError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed|Unable to preload CSS|not a valid JavaScript MIME type|Failed to load module script/i.test(message);
}

/** Recover a stale deployment chunk once per tab; never reload ordinary app errors. */
export function installAssetLoadRecovery(
  target: Window = window,
  reload: () => void = () => target.location.reload(),
): () => void {
  const handleError = (event: Event) => {
    const { payload } = event as Event & { payload?: unknown };
    if (!isAssetLoadError(payload)) return;
    try {
      // Keep this marker after reload so a broken deployment cannot cause a loop.
      // If storage is unavailable, let the route error page offer a manual retry.
      if (target.sessionStorage.getItem(RELOAD_KEY)) return;
      target.sessionStorage.setItem(RELOAD_KEY, "1");
    } catch {
      return;
    }
    event.preventDefault();
    // reload retains the current path, taskId query, and hash.
    reload();
  };
  target.addEventListener("vite:preloadError", handleError);
  return () => target.removeEventListener("vite:preloadError", handleError);
}
