import { useI18n } from "@/i18n/I18nProvider";

export function PageDraftNotice({ notice, onDiscard, disabled = false }: {
  notice: "restored" | "discarded" | "memory" | null;
  onDiscard: () => void;
  disabled?: boolean;
}) {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const message = notice === "memory"
    ? (zh ? "浏览器暂存不可用；当前内容仅在本次页面导航中保留，刷新后会丢失。" : "Browser storage is unavailable. Work is kept during navigation only; reloading will lose it.")
    : notice === "discarded"
      ? (zh ? "旧草稿已过期或无法读取，请重新填写。" : "The previous draft expired or could not be read. Start a new draft.")
      : notice === "restored"
        ? (zh ? "已恢复未提交草稿；未自动上传或开始识别。" : "Unsubmitted draft restored. No upload or recognition was started.")
        : (zh ? "本标签页自动暂存 8 小时；未上传文件刷新后需重新选择。" : "Drafts stay in this tab for 8 hours. Reselect files that have not uploaded after a reload.");
  return <div className="my-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-muted/40 px-4 py-3 text-xs text-muted-foreground">
    <p role={notice ? "status" : undefined} className="min-w-0 flex-1">{message}</p>
    <button type="button" disabled={disabled} onClick={() => {
      if (window.confirm(zh ? "放弃本页未提交草稿？已保存的任务内容不受影响。" : "Discard this page's unsubmitted draft? Saved task content is kept.")) onDiscard();
    }} className="shrink-0 rounded px-2 py-1 font-semibold text-foreground hover:bg-muted disabled:opacity-50">{zh ? "放弃草稿" : "Discard draft"}</button>
  </div>;
}
