const messages = {
  restored: ["草稿已恢复。", "Draft restored."],
  saved: ["草稿已保存。", "Draft saved."],
  expired: ["旧草稿已过期或版本不兼容，请重新填写。", "The saved draft expired or is incompatible. Enter the details again."],
  corrupt: ["草稿或文件已损坏，请重新填写并选择文件。", "The draft or its files are damaged. Enter the details and select the files again."],
  restoreConflict: ["发现旧草稿，当前内容已变化。请核对后选择是否恢复。", "A saved draft differs from the current content. Review it before restoring."],
  versionConflict: ["任务内容已更新，当前输入已保留。请选择保留当前输入或恢复旧草稿，再暂存。", "Task content has changed. Choose to keep your current input or restore the saved draft before saving."],
  remoteChange: ["另一标签页已更新草稿。当前输入已保留，请重新打开页面核对。", "Another tab updated the draft. Your input is preserved; reopen this page to review it."],
  cleared: ["已退出或切换账号，本机草稿已清除。", "Signed out or switched accounts. Local drafts have been cleared."],
  cleanupFailed: ["内容已保存，但旧草稿未清除。无需重复提交。", "Your changes were saved, but the old draft could not be cleared. No need to submit again."],
  conflictRestored: ["已恢复旧草稿，尚未正式保存。请核对后暂存或提交。", "Saved draft restored. Review it, then save the draft or submit your changes."],
  keptCurrent: ["已保留当前输入，请重新暂存。", "Current input kept. Save the draft again."],
  latestLoaded: ["已载入最新内容，之前暂存的草稿已保留。", "Latest content loaded. Your saved draft is still available."],
  deleted: ["已删除本页本地草稿。", "This page's local draft was deleted."],
  secret: ["密码和密钥不能暂存，请使用页面的保存操作或直接离开。", "Passwords and keys cannot be saved as drafts. Use the page's save action or leave without saving."],
  loading: ["正在读取草稿，请稍后再暂存。", "Loading the draft. Wait before saving."],
  invalidEditor: ["此编辑页面已失效，请重新打开后暂存。", "This editor is no longer active. Reopen the page before saving a draft."],
  unresolvedConflict: ["请先选择保留当前输入或恢复旧草稿，再暂存。", "Choose to keep your current input or restore the saved draft before saving."],
  busy: ["正在保存，请稍后再暂存或离开。", "Saving changes. Wait before saving a draft or leaving."],
  editorChanged: ["编辑内容已切换，请核对当前页面后再离开。", "The editor has changed. Review the current page before leaving."],
  changedDuringSave: ["暂存期间内容又有修改，请再次暂存后离开。", "Input changed while saving. Save the draft again before leaving."],
  transactionFailed: ["暂存未完成，请重试。", "The draft was not saved. Try again."],
  unavailable: ["此浏览器无法暂存草稿，请换用其他浏览器。", "Draft storage is unavailable in this browser. Try another browser."],
  blocked: ["其他标签页阻止了暂存，请关闭旧页面后重试。", "Another tab is blocking draft storage. Close older tabs and try again."],
  quota: ["浏览器存储空间不足，请删除旧草稿或释放空间后重试。", "Browser storage is full. Delete older drafts or free up space, then try again."],
  tooLarge: ["草稿超过 64 MiB，未暂存。请减少文件或内容后重试。", "The draft exceeds 64 MiB and was not saved. Reduce the files or content and try again."],
  unreadableFile: ["所选文件无法读取，请重新选择后暂存。", "The selected file could not be read. Select it again before saving a draft."],
  sessionChanged: ["登录状态已变化，请重新登录后暂存。", "Your sign-in status changed. Sign in again before saving a draft."],
  staleWrite: ["其他标签页已暂存或删除此草稿。请重新打开页面核对后暂存。", "Another tab saved or deleted this draft. Reopen the page and review it before saving."],
  limit: ["草稿已达到 30 份或 128 MiB 上限，请在历史任务的暂存草稿中删除旧草稿后重试。", "Drafts have reached the limit of 30 drafts or 128 MiB. Delete older drafts in Task history → Saved drafts, then try again."],
  failed: ["本地暂存失败，当前输入已保留，请重试。", "Could not save the local draft. Your input is preserved; try again."],
} as const;

export type DraftMessage = keyof typeof messages;
export function draftMessage(message: string, locale = "zh-CN"): string {
  const key = Object.hasOwn(messages, message) ? message as DraftMessage : "failed";
  return messages[key][locale === "en-US" ? 1 : 0];
}
export function draftErrorCode(error: unknown): DraftMessage {
  if (error instanceof DOMException && error.name === "QuotaExceededError") return "quota";
  return error instanceof Error && Object.hasOwn(messages, error.message) ? error.message as DraftMessage : "failed";
}
