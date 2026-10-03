import { draftGeneration, subscribeDraftChanges } from "./pageDraftStore";

const prefix = "smartai:image-return:";
const key = (owner: string, path: string) => prefix + JSON.stringify([owner, path]);
// A navigation choice only: no files, form autosave, credentials or model call.
// Written by explicit recovery/model-selection actions, scoped to this tab/user.
export function rememberImageReturn(owner: string | null, path: string, model: string) {
  if (!owner || !/^\/tasks\/[^/]+\/(?:upload\/problems|submissions\/upload)$/.test(path)) return;
  try {
    sessionStorage.setItem(key(owner, path), JSON.stringify({ model, epoch: draftGeneration(), expires: Date.now() + 30 * 60_000 }));
  } catch { /* Explicit return state still works if tab storage is unavailable. */ }
}

export function takeImageReturn(owner: string | null, path: string): string | null {
  if (!owner) return null;
  try {
    const item = sessionStorage.getItem(key(owner, path));
    sessionStorage.removeItem(key(owner, path));
    if (!item) return null;
    const value = JSON.parse(item);
    return value.epoch === draftGeneration() && value.expires > Date.now() && typeof value.model === "string" ? value.model : null;
  } catch { return null; }
}

subscribeDraftChanges(event => {
  if ((event as CustomEvent<{ type: string }>).detail.type !== "clear") return;
  try {
    for (const name of Object.keys(sessionStorage)) if (name.startsWith(prefix)) sessionStorage.removeItem(name);
  } catch { /* Storage can be disabled. */ }
});
