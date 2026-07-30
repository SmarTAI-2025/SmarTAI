import { Menu, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, Outlet, useNavigate } from "react-router-dom";
import { useCurrentUser, useExperts, useLogout } from "@/api/hooks";
import { AccountMenu } from "@/components/layout/AccountMenu";
import { LanguageToggle } from "@/components/layout/LanguageToggle";
import { ModelStatusMenu } from "@/components/layout/ModelStatusMenu";
import { PrimaryNavigation } from "@/components/layout/PrimaryNavigation";
import { useI18n } from "@/i18n/I18nProvider";

export function AppShell() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const mobileTriggerRef = useRef<HTMLButtonElement>(null);
  const mobileCloseRef = useRef<HTMLButtonElement>(null);
  const mobilePanelRef = useRef<HTMLElement>(null);
  const { t } = useI18n();
  const currentUser = useCurrentUser();
  const expertsQuery = useExperts();
  const logout = useLogout();
  const navigate = useNavigate();

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 1024px)");
    const closeOnDesktop = (event: MediaQueryListEvent) => {
      if (event.matches) {
        setMobileOpen(false);
      }
    };

    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, []);

  useEffect(() => {
    if (!mobileOpen) {
      return;
    }

    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    window.requestAnimationFrame(() => mobileCloseRef.current?.focus());

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMobileOpen(false);
        window.requestAnimationFrame(() => mobileTriggerRef.current?.focus());
        return;
      }

      if (event.key === "Tab") {
        const focusable = mobilePanelRef.current?.querySelectorAll<HTMLElement>(
          "a[href], button:not([disabled]), [tabindex]:not([tabindex='-1'])",
        );
        if (!focusable?.length) {
          return;
        }

        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [mobileOpen]);

  async function handleLogout() {
    try {
      await logout.mutateAsync();
    } finally {
      navigate("/login", { replace: true });
    }
  }

  const experts = expertsQuery.data ?? [];
  const enabledCount = experts.reduce(
    (count, expert) => count + (expert.enabled ? 1 : 0),
    0,
  );

  function closeMobileAndRestoreFocus() {
    setMobileOpen(false);
    window.requestAnimationFrame(() => mobileTriggerRef.current?.focus());
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      <a
        href="#main-content"
        className="sr-only left-4 top-3 z-[70] rounded-md bg-primary px-3 py-2 text-sm font-semibold text-primary-foreground focus:fixed focus:not-sr-only"
      >
        {t("skipToContent")}
      </a>

      <header className="sticky top-0 z-40 h-[70px] border-b bg-card">
        <div className="mx-auto flex h-full w-full max-w-[1440px] items-center px-5 sm:px-8 xl:px-[42px]">
          <button
            ref={mobileTriggerRef}
            type="button"
            className="mr-3 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring lg:hidden"
            onClick={() => setMobileOpen(true)}
            aria-label={t("openNavigation")}
            aria-expanded={mobileOpen}
          >
            <Menu aria-hidden="true" size={20} />
          </button>

          <Link
            to="/"
            className="shrink-0 text-[22px] font-bold leading-[27px] tracking-[-0.02em] text-primary outline-none focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
          >
            {t("appName")}
          </Link>

          <PrimaryNavigation className="ml-7 hidden lg:flex" />

          <div className="ml-auto flex min-w-0 items-center gap-1 sm:gap-2">
            <LanguageToggle />
            <ModelStatusMenu
              experts={experts}
              enabledCount={enabledCount}
              isLoading={expertsQuery.isLoading}
              isError={expertsQuery.isError}
              isFetching={expertsQuery.isFetching}
              onRetry={() => void expertsQuery.refetch()}
            />
            {currentUser.data ? (
              <AccountMenu
                user={currentUser.data}
                isSigningOut={logout.isPending}
                onSignOut={() => void handleLogout()}
              />
            ) : null}
          </div>
        </div>
      </header>

      {mobileOpen ? (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-slate-950/35"
            aria-label={t("closeNavigation")}
            onClick={closeMobileAndRestoreFocus}
          />
          <aside
            ref={mobilePanelRef}
            role="dialog"
            aria-modal="true"
            aria-label={t("mobileNavigation")}
            className="relative flex h-full w-[min(86vw,320px)] flex-col border-r bg-card shadow-2xl"
          >
            <div className="flex h-[70px] items-center justify-between border-b px-5">
              <Link
                to="/"
                className="text-[22px] font-bold leading-[27px] tracking-[-0.02em] text-primary"
                onClick={() => setMobileOpen(false)}
              >
                {t("appName")}
              </Link>
              <button
                ref={mobileCloseRef}
                type="button"
                className="inline-flex h-9 w-9 items-center justify-center rounded-lg text-muted-foreground outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                onClick={closeMobileAndRestoreFocus}
                aria-label={t("closeNavigation")}
              >
                <X aria-hidden="true" size={20} />
              </button>
            </div>
            <PrimaryNavigation
              mobile
              className="grid gap-1 p-3"
              onNavigate={() => setMobileOpen(false)}
            />
          </aside>
        </div>
      ) : null}

      <main
        id="main-content"
        tabIndex={-1}
        className="w-full px-5 py-[35px] outline-none sm:px-8"
      >
        <div className="mx-auto w-full max-w-[1300px]">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
