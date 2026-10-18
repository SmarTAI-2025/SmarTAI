import { putJSON } from "./client";
import type { Locale } from "@/i18n/messages";

export type ContentLanguage = "zh" | "en";
export const contentLanguageFromLocale = (locale: Locale): ContentLanguage => locale === "en-US" ? "en" : "zh";

/** Idempotent first-entry default. Later UI switches never overwrite it. */
export async function initializeContentLanguage(taskId: string, language: ContentLanguage) {
  // Keep the first proposal across a failed request/reload until the server
  // acknowledges it. Task IDs are unique; this stores no source or user content.
  const key = `smartai_content_language_pending:${taskId}`;
  try {
    const pending = sessionStorage.getItem(key);
    if (pending === "zh" || pending === "en") language = pending;
    else sessionStorage.setItem(key, language);
  } catch { /* Storage may be unavailable; the server still owns the default. */ }
  const result = await putJSON<{ language: ContentLanguage }>(`/tasks/${encodeURIComponent(taskId)}/content-language/default`, { language });
  try { sessionStorage.removeItem(key); } catch { /* Optional retry storage. */ }
  return result;
}
