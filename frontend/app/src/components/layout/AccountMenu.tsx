import { ChevronDown, KeyRound, Loader2, LogOut, Settings } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { HeaderPopover } from "@/components/layout/HeaderPopover";
import { useI18n } from "@/i18n/I18nProvider";
import { cn } from "@/lib/cn";
import type { User } from "@/types";

interface AccountMenuProps {
  user: User;
  isSigningOut: boolean;
  onSignOut: () => void;
}

/** Account actions intentionally kept out of the primary product navigation. */
export function AccountMenu({ user, isSigningOut, onSignOut }: AccountMenuProps) {
  const { t } = useI18n();
  const roleLabel = user.role === "admin" ? t("adminRole") : t("teacherRole");

  return (
    <HeaderPopover
      ariaLabel={t("userMenu")}
      triggerClassName="gap-1.5 px-1.5 sm:px-2.5"
      panelClassName="w-64"
      renderTrigger={(open) => (
        <>
          <span className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-primary/[0.1] text-xs font-bold text-primary sm:hidden">
            {userInitial(user.username)}
          </span>
          <span className="hidden max-w-44 truncate sm:inline">
            {user.username} · {roleLabel}
          </span>
          <ChevronDown
            aria-hidden="true"
            size={14}
            className={cn("hidden transition-transform sm:block", open && "rotate-180")}
          />
        </>
      )}
    >
      {(close) => (
        <>
          <div className="px-2 pb-2 pt-1">
            <p className="truncate text-sm font-semibold text-foreground">{user.username}</p>
            <p className="mt-0.5 truncate text-xs text-muted-foreground">
              {user.email || roleLabel}
            </p>
          </div>
          <div className="border-y py-1">
            <MenuLink
              to="/settings/account"
              label={t("accountSettings")}
              icon={<Settings aria-hidden="true" size={16} />}
              onClick={close}
            />
            <MenuLink
              to="/settings/byok"
              label={t("modelsAndByok")}
              icon={<KeyRound aria-hidden="true" size={16} />}
              onClick={close}
            />
          </div>
          <button
            type="button"
            data-popover-focus
            disabled={isSigningOut}
            onClick={() => {
              close();
              onSignOut();
            }}
            className="mt-1 flex w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-sm font-medium text-danger outline-none transition-colors hover:bg-danger/10 focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
          >
            {isSigningOut ? (
              <Loader2 aria-hidden="true" size={16} className="animate-spin" />
            ) : (
              <LogOut aria-hidden="true" size={16} />
            )}
            {t("signOut")}
          </button>
        </>
      )}
    </HeaderPopover>
  );
}

function MenuLink({
  to,
  label,
  icon,
  onClick,
}: {
  to: string;
  label: string;
  icon: ReactNode;
  onClick: () => void;
}) {
  return (
    <Link
      to={to}
      data-popover-focus
      onClick={onClick}
      className="flex items-center gap-2 rounded-lg px-2 py-2 text-sm font-medium text-foreground outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
    >
      <span className="text-muted-foreground">{icon}</span>
      {label}
    </Link>
  );
}

function userInitial(username: string): string {
  return username.trim().slice(0, 1).toUpperCase() || "U";
}
