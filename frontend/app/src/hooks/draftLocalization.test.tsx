import "fake-indexeddb/auto";
import { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { I18nProvider, useI18n } from "@/i18n/I18nProvider";
import { DraftActions, LocalizedDraftLeaveProvider } from "./useDraftLeave";
import { PageDraftSession, useDraftProtection } from "./useDraftProtection";
import * as store from "@/lib/pageDraftStore";
import { draftMessage } from "@/lib/draftMessages";

const leave = vi.fn();
function Editor({ secret = false }: { secret?: boolean }) {
  const { locale, setLocale } = useI18n();
  const [value, setValue] = useState({ name: "" });
  const [version, setVersion] = useState("1");
  const draft = useDraftProtection({ scope: "bilingual", value, version, secret, onRestore: setValue });
  return <>
    <input aria-label="input" value={value.name} onChange={e => setValue({ name: e.target.value })} />
    <button onClick={() => setVersion("2")}>Server update</button>
    <button onClick={() => setLocale(locale === "en-US" ? "zh-CN" : "en-US")}>Switch language</button>
    <button onClick={() => draft.requestLeave(leave)}>Leave editor</button>
    <DraftActions />
  </>;
}
function setup(locale = "en-US", secret = false) {
  localStorage.setItem("smartai_locale", locale);
  return render(<I18nProvider><PageDraftSession ownerId="bilingual-owner"><LocalizedDraftLeaveProvider><Editor secret={secret} /></LocalizedDraftLeaveProvider></PageDraftSession></I18nProvider>);
}
async function ready() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Save draft" })).toBeEnabled());
}
async function save() {
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  await screen.findByText(/Draft saved ·/);
}
beforeEach(async () => {
  vi.restoreAllMocks(); leave.mockReset(); localStorage.clear(); await store.clearPageDrafts();
});

it("renders an English conflict and keeps current input until the user resolves it", async () => {
  setup(); await ready();
  fireEvent.change(screen.getByLabelText("input"), { target: { value: "saved" } }); await save();
  fireEvent.change(screen.getByLabelText("input"), { target: { value: "current" } });
  fireEvent.click(screen.getByText("Server update"));
  expect(await screen.findByText(draftMessage("versionConflict", "en-US"))).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(draftMessage("unresolvedConflict", "en-US"));
  expect(screen.getByLabelText("input")).toHaveValue("current");
  expect(document.body.textContent).not.toMatch(/[\u3400-\u9fff]/);
  fireEvent.click(screen.getByRole("button", { name: "Restore preserved content" }));
  expect(screen.getByLabelText("input")).toHaveValue("saved");
  await save();
  expect((await store.readPageDraft("bilingual-owner", "bilingual", store.objectDraftCodec())).value).toEqual({ name: "saved" });
  expect(leave).not.toHaveBeenCalled();
});

it("translates an existing notice and save error without reading the draft again or changing input", async () => {
  const read = vi.spyOn(store, "readPageDraft");
  setup(); await ready();
  fireEvent.change(screen.getByLabelText("input"), { target: { value: "current" } });
  fireEvent.click(screen.getByText("Server update"));
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  await screen.findByRole("alert");
  const reads = read.mock.calls.length;
  fireEvent.click(screen.getByText("Switch language"));
  expect(screen.getByText(draftMessage("versionConflict", "zh-CN"))).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent(draftMessage("unresolvedConflict", "zh-CN"));
  expect(screen.getByLabelText("input")).toHaveValue("current");
  expect(read).toHaveBeenCalledTimes(reads);
  fireEvent.click(screen.getByRole("button", { name: "保留当前输入" }));
  fireEvent.click(screen.getByText("Switch language"));
  expect(screen.getByText(draftMessage("keptCurrent", "en-US"))).toBeVisible();
  await save();
});

it("keeps the English leave dialog and input open on storage failure, then saves and leaves once", async () => {
  setup(); await ready();
  fireEvent.change(screen.getByLabelText("input"), { target: { value: "keep" } });
  const write = vi.spyOn(store, "writePageDrafts").mockRejectedValueOnce(new DOMException("raw IndexedDB detail", "QuotaExceededError"));
  fireEvent.click(screen.getByText("Leave editor"));
  let dialog = await screen.findByRole("alertdialog");
  expect(dialog).toHaveAccessibleName("Unsaved changes");
  expect(dialog).toHaveTextContent("Saving a draft does not start uploading, recognition or grading.");
  fireEvent.click(within(dialog).getByRole("button", { name: "Save draft and leave" }));
  await waitFor(() => expect(dialog).toHaveTextContent(draftMessage("quota", "en-US")));
  expect(dialog.textContent).not.toMatch(/[\u3400-\u9fff]/);
  expect(screen.getByLabelText("input")).toHaveValue("keep");
  expect(leave).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Switch language"));
  dialog = screen.getByRole("alertdialog");
  expect(dialog).toHaveAccessibleName("有未暂存修改");
  expect(dialog).toHaveTextContent(draftMessage("quota", "zh-CN"));
  fireEvent.click(within(dialog).getByRole("button", { name: "暂存并离开" }));
  await waitFor(() => expect(leave).toHaveBeenCalledTimes(1));
  expect(write).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

it("keeps secrets out of draft storage with a fully English leave dialog", async () => {
  setup("en-US", true);
  fireEvent.change(screen.getByLabelText("input"), { target: { value: "secret" } });
  fireEvent.click(screen.getByText("Leave editor"));
  const dialog = await screen.findByRole("alertdialog");
  expect(dialog).toHaveTextContent("Passwords and keys cannot be saved as drafts.");
  expect(within(dialog).queryByRole("button", { name: "Save draft and leave" })).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "Keep editing" }));
  expect(screen.getByLabelText("input")).toHaveValue("secret");
  expect(await store.listPageDrafts("bilingual-owner")).toEqual([]);
  expect(leave).not.toHaveBeenCalled();
});

it("localizes draft restoration and hides unexpected storage details", async () => {
  const codec = store.objectDraftCodec<{ name: string }>();
  const loaded = await store.readPageDraft("bilingual-owner", "bilingual", codec);
  await store.writePageDrafts([store.preparePageDraft("bilingual-owner", "bilingual", "1", { name: "restored" }, codec, loaded.epoch, null)]);
  let view = setup(); await ready();
  await waitFor(() => expect(screen.getByLabelText("input")).toHaveValue("restored"));
  expect(screen.getByText("Draft restored.")).toBeVisible();
  view.unmount();
  vi.spyOn(store, "readPageDraft").mockRejectedValueOnce(new Error("Unexpected storage implementation detail"));
  view = setup(); await ready();
  expect(screen.getByText(draftMessage("failed", "en-US"))).toBeVisible();
  expect(document.body.textContent).not.toContain("implementation detail");
  await act(async () => view.unmount());
});
