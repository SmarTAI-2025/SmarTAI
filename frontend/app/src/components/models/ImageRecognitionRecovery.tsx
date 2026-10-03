import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useDraftLeave, type DraftController } from "@/hooks/useDraftLeave";
import { getAPIErrorCode } from "@/api/client";
import type { ExpertConfig } from "@/types";

const imageFailureCodes = new Set(["provider_vision_not_supported", "visual_capability_unavailable", "vision_provider_required", "image_recognition_unconfirmed", "ocr_empty_result"]);
export function needsImageRecovery(error: unknown) { return imageFailureCodes.has(getAPIErrorCode(error) ?? ""); }

// Restore first, then apply the latest explicit choice once. No model request.
export function useImageRecoveryReturn(loaded: boolean, onSelect: (id: string) => void) {
  const location = useLocation();
  const applied = useRef<string | null>(null);
  useEffect(() => {
    if (!loaded || applied.current === location.key) return;
    applied.current = location.key;
    if (typeof location.state?.imageRecoveryModel === "string" && location.state.imageRecoveryModel) onSelect(location.state.imageRecoveryModel);
  }, [loaded, location.key, location.state, onSelect]);
}

export function ImageRecognitionRecovery({ error, expert, returnTo, controller, isCurrent, locale }: {
  error: unknown; expert?: ExpertConfig; returnTo: string; controller: () => DraftController;
  isCurrent: () => boolean; locale: "zh-CN" | "en-US";
}) {
  const navigate = useNavigate();
  const leave = useDraftLeave();
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
      await new Promise<void>(resolve => window.setTimeout(resolve, 0));
      const current = controller();
      if (!isCurrent() || current.id !== original.id || current.dirty || current.busy || current.savedAt === null) throw new Error(zh ? "内容有修改或尚未暂存成功，请再次暂存；输入仍保留。" : "Input changed or could not be saved. Your input is preserved.");
      const params = new URLSearchParams({ returnTo, imageRecovery: "1", mode });
      if (expert) params.set("providerId", expert.provider_id);
      navigate(`/settings/byok?${params}`);
    } catch (failure) {
      setSaveError(failure instanceof Error ? failure.message : zh ? "暂存失败，输入仍保留。" : "Saving failed; input is preserved.");
    } finally { busy.current = false; setSaving(false); }
  }
  return <div role="alert" className="mt-4 rounded-lg border border-amber-300 bg-card p-4 text-sm">
    <p>{passed
      ? (zh ? "当前文件需要图片识别，所选模型已通过图片能力验证。识别效果差可能来自文件模糊或模型识别效果，请换清晰文件或换模型后继续。离开前会暂存文件和填写内容，返回后无需重新上传。" : "This file needs image recognition. The model passed image verification. Poor results may come from a blurry file or model accuracy. Try a clearer file or another model; files and fields will be saved before leaving.")
      : unsupported
        ? (zh ? "当前文件需要图片识别，但所选模型的当前配置不支持图片输入。请更换模型后继续。离开前会暂存文件和填写内容，返回后无需重新上传。" : "This file needs image recognition, but the current configuration explicitly rejects images. Change models to continue; files and fields will be saved before leaving.")
        : (zh ? "当前文件需要图片识别，暂时无法确认所选模型能否处理图片。你可以验证图片能力，或更换模型后继续。离开前会暂存文件和填写内容，返回后无需重新上传。" : "This file needs image recognition; image support is uncertain. Verify or change models. Files and fields will be saved before leaving.")}</p>
    <div className="mt-3 flex flex-wrap gap-2">
      {!unsupported && !passed ? <button type="button" disabled={saving || leave.saving} onClick={() => void saveAndGo("verify")} className="rounded border px-3 py-2">{zh ? "暂存并去验证" : "Save and verify"}</button> : null}
      <button type="button" disabled={saving || leave.saving} onClick={() => void saveAndGo("change")} className="rounded border px-3 py-2">{zh ? "暂存并更换模型" : "Save and change model"}</button>
      <button type="button" disabled={saving} onClick={() => setDismissed(true)} className="rounded border px-3 py-2">{zh ? "留在当前页" : "Stay here"}</button>
    </div>
    {saving ? <p role="status">{zh ? "正在暂存…" : "Saving…"}</p> : null}
    {saveError ? <p className="mt-2 text-danger">{saveError}</p> : null}
  </div>;
}
