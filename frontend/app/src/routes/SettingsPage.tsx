import { Languages, MonitorCog, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";
import { useCurrentUser } from "@/api/hooks";
import { useI18n } from "@/i18n/I18nProvider";
import type { Locale } from "@/i18n/messages";
import { cn } from "@/lib/cn";
import { type ThemeMode, useTheme } from "@/theme/ThemeProvider";

const themeOptions: Array<{ value: ThemeMode; zh: string; en: string }> = [
  { value: "light", zh: "浅色", en: "Light" },
  { value: "dark", zh: "深色", en: "Dark" },
  { value: "system", zh: "跟随系统", en: "System" },
];

export function SettingsPage() {
  const { locale, setLocale } = useI18n();
  const { theme, setTheme } = useTheme();
  const userQuery = useCurrentUser();
  const user = userQuery.data;

  return (
    <div>
      <header>
        <h1 className="text-[28px] font-bold leading-[38px] tracking-[-0.025em] text-foreground">
          {tx(locale, "账户设置", "Account settings")}
        </h1>
        <p className="mt-1 text-[13px] leading-5 text-muted-foreground">
          {tx(
            locale,
            "查看当前登录账号，并调整这台设备上的界面偏好。",
            "Review the signed-in account and adjust interface preferences for this device.",
          )}
        </p>
      </header>

      <section
        aria-labelledby="account-profile-title"
        className="mt-7 overflow-hidden rounded-[10px] border bg-card"
      >
        <div className="flex flex-col gap-4 border-b px-5 py-5 sm:flex-row sm:items-center sm:px-6">
          <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-primary/[0.1] text-lg font-bold text-primary">
            {userInitial(user?.username)}
          </span>
          <div className="min-w-0">
            <h2
              id="account-profile-title"
              className="truncate text-[18px] font-bold leading-6 text-foreground"
            >
              {user?.username ?? tx(locale, "正在读取账号…", "Loading account…")}
            </h2>
            <p className="mt-1 text-[12px] leading-5 text-muted-foreground">
              {tx(
                locale,
                "账号资料由当前登录会话提供，不在此页面伪造可编辑能力。",
                "Account details come from the active session; unsupported profile editing is not shown.",
              )}
            </p>
          </div>
          <span className="sm:ml-auto inline-flex h-7 w-fit items-center rounded-full bg-emerald-50 px-3 text-[11px] font-semibold text-emerald-700">
            {tx(locale, "已登录", "Signed in")}
          </span>
        </div>

        <dl className="grid sm:grid-cols-3">
          <AccountFact
            label={tx(locale, "用户名", "Username")}
            value={user?.username ?? "—"}
          />
          <AccountFact
            label={tx(locale, "邮箱", "Email")}
            value={user?.email || tx(locale, "未设置", "Not set")}
            divided
          />
          <AccountFact
            label={tx(locale, "账号角色", "Account role")}
            value={
              user
                ? roleLabel(locale, user.role)
                : "—"
            }
            divided
          />
        </dl>
      </section>

      <section
        aria-labelledby="preferences-title"
        className="mt-5 overflow-hidden rounded-[10px] border bg-card"
      >
        <div className="border-b px-5 py-4 sm:px-6">
          <h2 id="preferences-title" className="text-[17px] font-bold text-foreground">
            {tx(locale, "界面偏好", "Interface preferences")}
          </h2>
          <p className="mt-1 text-[12px] leading-5 text-muted-foreground">
            {tx(
              locale,
              "修改后立即生效，并只保存在当前浏览器。",
              "Changes apply immediately and are stored only in this browser.",
            )}
          </p>
        </div>

        <PreferenceRow
          icon={<Languages aria-hidden="true" className="h-[18px] w-[18px]" />}
          title={tx(locale, "界面语言", "Interface language")}
          description={tx(
            locale,
            "切换导航、状态和操作文案。",
            "Switch navigation, status, and action labels.",
          )}
        >
          <SegmentedControl
            ariaLabel={tx(locale, "选择界面语言", "Select interface language")}
            options={[
              { value: "zh-CN", label: "中文" },
              { value: "en-US", label: "English" },
            ]}
            value={locale}
            onChange={(value) => setLocale(value as Locale)}
          />
        </PreferenceRow>

        <PreferenceRow
          icon={<MonitorCog aria-hidden="true" className="h-[18px] w-[18px]" />}
          title={tx(locale, "外观", "Appearance")}
          description={tx(
            locale,
            "选择浅色、深色或跟随系统。",
            "Choose light, dark, or your system setting.",
          )}
          divided
        >
          <SegmentedControl
            ariaLabel={tx(locale, "选择界面外观", "Select interface appearance")}
            options={themeOptions.map((option) => ({
              value: option.value,
              label: locale === "zh-CN" ? option.zh : option.en,
            }))}
            value={theme}
            onChange={(value) => setTheme(value as ThemeMode)}
          />
        </PreferenceRow>
      </section>

      <div className="mt-5 flex items-start gap-3 rounded-[10px] border bg-card px-5 py-4 text-[12px] leading-5 text-muted-foreground sm:px-6">
        <ShieldCheck aria-hidden="true" className="mt-0.5 h-[18px] w-[18px] shrink-0 text-emerald-600" />
        <div>
          <p className="font-semibold text-foreground">
            {tx(locale, "账号与模型设置彼此独立", "Account and model settings are separate")}
          </p>
          <p className="mt-0.5">
            {tx(
              locale,
              "API key 请从右上角用户名菜单进入“模型与 BYOK”管理；退出登录也始终位于该菜单底部。",
              "Manage API keys from “Models & BYOK” in the account menu; sign out remains at the bottom of that menu.",
            )}
          </p>
        </div>
      </div>
    </div>
  );
}

function AccountFact({
  label,
  value,
  divided = false,
}: {
  label: string;
  value: string;
  divided?: boolean;
}) {
  return (
    <div className={cn("min-w-0 px-5 py-4 sm:px-6", divided && "border-t sm:border-l sm:border-t-0")}>
      <dt className="text-[11px] font-medium text-muted-foreground">{label}</dt>
      <dd className="mt-1 truncate text-[15px] font-bold text-foreground" title={value}>
        {value}
      </dd>
    </div>
  );
}

function PreferenceRow({
  icon,
  title,
  description,
  children,
  divided = false,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  children: ReactNode;
  divided?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-4 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6",
        divided && "border-t",
      )}
    >
      <div className="flex min-w-0 items-start gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[8px] bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300">
          {icon}
        </span>
        <div>
          <h3 className="text-[14px] font-bold text-foreground">{title}</h3>
          <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{description}</p>
        </div>
      </div>
      {children}
    </div>
  );
}

function SegmentedControl({
  ariaLabel,
  options,
  value,
  onChange,
}: {
  ariaLabel: string;
  options: Array<{ value: string; label: string }>;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="grid min-w-0 grid-flow-col auto-cols-fr gap-1 rounded-[8px] bg-slate-100 p-1 dark:bg-slate-800 sm:min-w-[280px]"
    >
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={selected}
            onClick={() => onChange(option.value)}
            className={cn(
              "h-8 rounded-[6px] px-3 text-[12px] font-semibold text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring",
              selected && "bg-card text-primary shadow-[0_1px_3px_rgba(15,23,42,0.08)]",
            )}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

function userInitial(username?: string): string {
  return username?.trim().slice(0, 1).toUpperCase() || "U";
}

function roleLabel(locale: Locale, role: string): string {
  if (role === "admin") return tx(locale, "管理员", "Administrator");
  if (role === "student") return tx(locale, "学生", "Student");
  return tx(locale, "教师", "Teacher");
}

function tx(locale: Locale, zh: string, en: string): string {
  return locale === "zh-CN" ? zh : en;
}
