import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import { messages, type Locale, type MessageKey } from "@/i18n/messages";

const LOCALE_KEY = "smartai_locale";

interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

function getInitialLocale(): Locale {
  const stored = window.localStorage.getItem(LOCALE_KEY);
  return stored === "en-US" ? "en-US" : "zh-CN";
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(getInitialLocale);

  const value = useMemo<I18nContextValue>(() => {
    function setLocale(next: Locale) {
      window.localStorage.setItem(LOCALE_KEY, next);
      setLocaleState(next);
    }

    return {
      locale,
      setLocale,
      t: (key) => messages[locale][key] ?? messages["zh-CN"][key] ?? key,
    };
  }, [locale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const context = useContext(I18nContext);
  if (!context) {
    throw new Error("useI18n must be used within I18nProvider");
  }
  return context;
}

