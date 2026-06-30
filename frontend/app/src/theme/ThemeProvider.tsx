import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type ThemeMode = "light" | "dark" | "system";

const THEME_KEY = "smartai_theme";

interface ThemeContextValue {
  theme: ThemeMode;
  setTheme: (theme: ThemeMode) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function getInitialTheme(): ThemeMode {
  const stored = window.localStorage.getItem(THEME_KEY);
  if (stored === "light" || stored === "dark" || stored === "system") return stored;
  return "system";
}

function applyTheme(theme: ThemeMode) {
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const dark = theme === "dark" || (theme === "system" && prefersDark);
  document.documentElement.classList.toggle("dark", dark);
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeMode>(getInitialTheme);

  useEffect(() => {
    applyTheme(theme);
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const listener = () => applyTheme(theme);
    media.addEventListener("change", listener);
    return () => media.removeEventListener("change", listener);
  }, [theme]);

  const value = useMemo<ThemeContextValue>(() => {
    function setTheme(next: ThemeMode) {
      window.localStorage.setItem(THEME_KEY, next);
      setThemeState(next);
    }
    return { theme, setTheme };
  }, [theme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used within ThemeProvider");
  }
  return context;
}

