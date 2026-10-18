import { useEffect, useRef, useState } from "react";
import { contentLanguageFromLocale, initializeContentLanguage, type ContentLanguage } from "@/api/contentLanguage";
import { useI18n } from "@/i18n/I18nProvider";

export function useTaskContentLanguage(taskId?: string, enabled = true) {
  const { locale } = useI18n();
  const firstLanguage = useRef(contentLanguageFromLocale(locale));
  const [language, setLanguage] = useState<ContentLanguage | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!taskId || !enabled) return;
    let active = true;
    setError(null);
    void initializeContentLanguage(taskId, firstLanguage.current).then(
      result => { if (active) setLanguage(result.language); },
      failure => { if (active) setError(failure); },
    );
    return () => { active = false; };
  }, [taskId, enabled, attempt]);
  return { language, error, retry: () => setAttempt(value => value + 1) };
}
