import { AuthCard, AuthFrame } from "@/components/auth/AuthFrame";
import { useI18n } from "@/i18n/I18nProvider";

export function SessionRestoreError({ retry, busy }: { retry: () => void; busy: boolean }) {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  return (
    <AuthFrame>
      <AuthCard>
        <p role="alert" className="text-sm text-muted-foreground">
          {zh ? "暂时无法确认登录状态，请检查网络后重试。" : "Unable to check your session right now. Check your connection and retry."}
        </p>
        <button type="button" disabled={busy} onClick={retry} className="mt-4 rounded-lg bg-primary px-4 py-2 text-sm text-primary-foreground disabled:opacity-50">
          {busy ? (zh ? "正在重试…" : "Retrying…") : (zh ? "重试" : "Retry")}
        </button>
      </AuthCard>
    </AuthFrame>
  );
}
