import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useDraftLeave, type DraftController } from "@/hooks/useDraftLeave";
import { useDraftOwner } from "@/hooks/useDraftProtection";
import { rememberImageReturn, takeImageReturn } from "@/lib/imageRecoveryNavigation";
import { draftErrorCode, draftMessage } from "@/lib/draftMessages";
import { getAPIErrorCode } from "@/api/client";
import type { ExpertConfig } from "@/types";

const imageFailureCodes = new Set(["provider_vision_not_supported", "visual_capability_unavailable", "vision_provider_required", "image_recognition_unconfirmed", "ocr_empty_result"]);
export function needsImageRecovery(error: unknown) { return imageFailureCodes.has(getAPIErrorCode(error) ?? ""); }

// Restore first, then apply the latest explicit choice once. No model request.
export function useImageRecoveryReturn(loaded: boolean, onSelect: (id: string) => void) {
  const location = useLocation();
  const owner = useDraftOwner();
  const applied = useRef<string | null>(null);
  useEffect(() => {
    if (!loaded || applied.current === location.key) return;
    applied.current = location.key;
    const choice = takeImageReturn(owner, location.pathname);
    const model = choice || location.state?.imageRecoveryModel;
    if (typeof model === "string" && model) onSelect(model);
  }, [loaded, location.key, location.pathname, location.state, owner, onSelect]);
}

export function ImageRecognitionRecovery({ error, expert, returnTo, controller, isCurrent, locale }: {
  error: unknown; expert?: ExpertConfig; returnTo: string; controller: () => DraftController;
  isCurrent: () => boolean; locale: "zh-CN" | "en-US";
}) {
  const navigate = useNavigate();
  const leave = useDraftLeave();
  const owner = useDraftOwner();
  const busy = useRef(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState(false);
  useEffect(() => setDismissed(false), [error]);
  if (!needsImageRecovery(error) || dismissed) return null;
  const zh = locale === "zh-CN";
  const unsupported = getAPIErrorCode(error) === "provider_vision_not_supported" || expert?.image_capability_status === "unsupported";
  const passed = !unsupported && expert?.image_capability_status === "passed";
  async function saveAndGo(mode: "verify" | "change") {
    if (busy.current || leave.saving) return;
    busy.current = true; setSaving(true); setSaveError(null);
    try {
      const original = controller();
      await leave.save([original.id]);
      const current = controller();
      if (!isCurrent() || current.id !== original.id || current.dirty || current.busy || current.savedAt === null) throw new Error("changedDuringSave");
      const params = new URLSearchParams({ returnTo, imageRecovery: "1", mode });
      if (expert) params.set("providerId", expert.provider_id);
      rememberImageReturn(owner, returnTo, expert?.provider_id ?? "");
      navigate(`/settings/byok?${params}`);
    } catch (failure) {
      setSaveError(draftErrorCode(failure));
    } finally { busy.current = false; setSaving(false); }
  }
  return <div role="alert" className="mt-4 rounded-lg border border-amber-300 bg-card p-4 text-sm">
    <p>{passed
      ? (zh ? "图片未识别成功。请换清晰文件或更换模型；更换前会暂存文件和填写内容。" : "Image recognition failed. Try a clearer file or another model. Your file and input will be saved before switching.")
      : unsupported
        ? (zh ? "所选模型不支持图片输入，请更换模型。更换前会暂存文件和填写内容。" : "This model does not support image input. Change models to continue; your file and input will be saved first.")
        : (zh ? "请验证模型的图片识别能力，或更换模型。离开前会暂存文件和填写内容。" : "Verify image support or change models. Your file and input will be saved before leaving.")}</p>
    <div className="mt-3 flex flex-wrap gap-2">
      {!unsupported && !passed ? <button type="button" disabled={saving || leave.saving} onClick={() => void saveAndGo("verify")} className="rounded border px-3 py-2">{zh ? "暂存并去验证" : "Save and verify"}</button> : null}
      <button type="button" disabled={saving || leave.saving} onClick={() => void saveAndGo("change")} className="rounded border px-3 py-2">{zh ? "暂存并更换模型" : "Save and change model"}</button>
      <button type="button" disabled={saving} onClick={() => setDismissed(true)} className="rounded border px-3 py-2">{zh ? "留在当前页" : "Stay here"}</button>
    </div>
    {saving ? <p role="status">{zh ? "正在暂存…" : "Saving…"}</p> : null}
    {saveError ? <p className="mt-2 text-danger">{draftMessage(saveError, locale)}</p> : null}
  </div>;
}
