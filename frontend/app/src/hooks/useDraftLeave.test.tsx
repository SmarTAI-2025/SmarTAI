import "fake-indexeddb/auto";
import { Blob, File } from "node:buffer";
import { useEffect, useState } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, Link, Outlet, RouterProvider } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { DraftActions, DraftLeaveProvider } from "./useDraftLeave";
import { PageDraftSession, useDraftProtection } from "./useDraftProtection";
import * as store from "@/lib/pageDraftStore";

let version = "1";
let busy = false;
let secret = false;
const closed = vi.fn();
function Form() {
  const [value, setValue] = useState({ name: "server", file: null as File | null });
  const draft = useDraftProtection({ scope: "test:task:field", value, baseline: { name: "server", file: null as File | null }, version, busy, secret, onRestore: setValue });
  return <><input aria-label="name" value={value.name} onChange={e => setValue({ ...value, name: e.target.value })} /><Link to="/next">next</Link><button onClick={() => draft.requestLeave(closed)}>close editor</button><button onClick={() => void draft.clear()}>formal success</button><button onClick={() => { busy = !busy; setValue({ ...value }); }}>toggle busy</button><button onClick={() => { version = "2"; setValue({ ...value }); }}>server changes</button></>;
}
function setup(owner = "draft-teacher") {
  const router = createMemoryRouter([{ element: <DraftLeaveProvider><Outlet /><DraftActions /></DraftLeaveProvider>, children: [{ path: "/", element: <Form /> }, { path: "/next", element: <p>destination</p> }] }], { initialEntries: ["/"] });
  return { ...render(<PageDraftSession ownerId={owner}><RouterProvider router={router} /></PageDraftSession>), router };
}
async function ready() { await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled()); }
function type(name: string) { fireEvent.change(screen.getByLabelText("name"), { target: { value: name } }); }
async function save() { await ready(); fireEvent.click(screen.getByRole("button", { name: "暂存" })); await screen.findByText(/已暂存 ·/); }
beforeEach(async () => { vi.restoreAllMocks(); vi.stubGlobal("Blob", Blob); vi.stubGlobal("File", File); await store.clearPageDrafts(); version = "1"; busy = false; secret = false; closed.mockReset(); });

it("keeps the original destination and previous explicit snapshot for all three choices", async () => {
  const { router } = setup(); await ready(); type("explicit"); await save(); type("unwritten");
  fireEvent.click(screen.getByText("next")); fireEvent.click(await screen.findByRole("button", { name: "继续编辑" })); expect(screen.getByLabelText("name")).toHaveValue("unwritten");
  fireEvent.click(screen.getByText("next")); fireEvent.click(await screen.findByRole("button", { name: "不暂存并离开" })); await screen.findByText("destination");
  await act(async () => { await router.navigate(-1); }); await waitFor(() => expect(screen.getByLabelText("name")).toHaveValue("explicit"));
  type("new explicit"); fireEvent.click(screen.getByText("next")); fireEvent.click(await screen.findByRole("button", { name: "暂存并离开" })); await screen.findByText("destination");
  await act(async () => { await router.navigate(-1); }); await waitFor(() => expect(screen.getByLabelText("name")).toHaveValue("new explicit"));
});
it("storage failure stays, keeps the intended target and input, and retries exactly once", async () => {
  setup(); await ready(); type("keep"); const write = vi.spyOn(store, "writePageDrafts").mockRejectedValueOnce(new DOMException("full", "QuotaExceededError"));
  fireEvent.click(screen.getByText("next")); fireEvent.click(await screen.findByRole("button", { name: "暂存并离开" }));
  await screen.findAllByText(/存储空间不足/); expect(screen.getByLabelText("name")).toHaveValue("keep"); expect(screen.queryByText("destination")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "暂存并离开" })); await screen.findByText("destination"); expect(write).toHaveBeenCalledTimes(2);
});
it("edits during saving remain dirty; repeated navigation clicks do not replace the original intent", async () => {
  setup(); await ready(); type("snapshot"); let release!: () => void; const original = store.writePageDrafts;
  vi.spyOn(store, "writePageDrafts").mockImplementationOnce(async writes => { await new Promise<void>(resolve => { release = resolve; }); await original(writes); });
  fireEvent.click(screen.getByText("next")); fireEvent.click(await screen.findByRole("button", { name: "暂存并离开" })); await waitFor(() => expect(release).toBeTypeOf("function"));
  type("later edit"); fireEvent.click(screen.getByText("close editor")); await act(async () => release());
  await screen.findAllByText(/暂存期间内容又有修改/); expect(screen.getByLabelText("name")).toHaveValue("later edit"); expect(closed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "暂存并离开" })); await screen.findByText("destination"); expect(closed).not.toHaveBeenCalled();
});
it("editor close uses the same save, and Escape cancels without losing input", async () => {
  setup(); await ready(); type("editor"); fireEvent.click(screen.getByText("close editor")); await screen.findByRole("alertdialog"); fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("alertdialog")).toBeNull(); expect(screen.getByLabelText("name")).toHaveValue("editor");
  fireEvent.click(screen.getByText("close editor")); fireEvent.click(await screen.findByRole("button", { name: "暂存并离开" })); await waitFor(() => expect(closed).toHaveBeenCalledTimes(1)); await screen.findByText(/已暂存 ·/);
});
it("native beforeunload is present only for unsaved changes or business work", async () => {
  setup(); await ready(); const event = () => new Event("beforeunload", { cancelable: true }); let e = event(); window.dispatchEvent(e); expect(e.defaultPrevented).toBe(false);
  type("dirty"); e = event(); window.dispatchEvent(e); expect(e.defaultPrevented).toBe(true); await save(); e = event(); window.dispatchEvent(e); expect(e.defaultPrevented).toBe(false);
  fireEvent.click(screen.getByText("toggle busy")); e = event(); window.dispatchEvent(e); expect(e.defaultPrevented).toBe(true);
});
it("server changes preserve current input, demand explicit conflict resolution, and never submit", async () => {
  setup(); await ready(); type("old local"); await save(); type("new input"); fireEvent.click(screen.getByText("server changes"));
  expect(screen.getByLabelText("name")).toHaveValue("new input"); await screen.findByText(/服务器业务版本已变化/); fireEvent.click(screen.getByRole("button", { name: "暂存" })); await screen.findByText(/请先核对服务器变化/);
  fireEvent.click(screen.getByText("核对后恢复旧草稿")); expect(screen.getByLabelText("name")).toHaveValue("old local"); await save();
});
it("formal success clears the snapshot and fences a delayed prepared save", async () => {
  const { router } = setup(); await ready(); type("saved"); await save(); type("pending"); const original = store.writePageDrafts; let release!: () => void;
  vi.spyOn(store, "writePageDrafts").mockImplementationOnce(async writes => { await new Promise<void>(resolve => { release = resolve; }); await original(writes); });
  fireEvent.click(screen.getByRole("button", { name: "暂存" })); await waitFor(() => expect(release).toBeTypeOf("function")); fireEvent.click(screen.getByText("formal success"));
  await waitFor(async () => expect((await store.readPageDraft("draft-teacher", "test:task:field", store.objectDraftCodec())).value).toBeNull()); await act(async () => release());
  await screen.findByText(/其他标签页已暂存或删除/); await act(async () => { await router.navigate("/next"); }); await screen.findByText("destination"); await act(async () => { await router.navigate(-1); }); await waitFor(() => expect(screen.getByLabelText("name")).toHaveValue("server"));
});
it("session invalidation cancels a pending leave and prevents late snapshots", async () => {
  setup(); await ready(); type("account A"); const original = store.writePageDrafts; let release!: () => void;
  vi.spyOn(store, "writePageDrafts").mockImplementationOnce(async writes => { await new Promise<void>(resolve => { release = resolve; }); await original(writes); });
  fireEvent.click(screen.getByText("close editor")); fireEvent.click(await screen.findByRole("button", { name: "暂存并离开" })); await waitFor(() => expect(release).toBeTypeOf("function"));
  await act(async () => { await store.clearPageDrafts(); release(); }); expect(closed).not.toHaveBeenCalled(); expect(screen.queryByRole("alertdialog")).toBeNull(); expect(await store.listPageDrafts("draft-teacher")).toEqual([]);
});
it("authentication secrets have leave protection but no draft save option or storage", async () => {
  secret = true; setup(); type("secret"); fireEvent.click(screen.getByText("close editor")); await screen.findByRole("alertdialog"); expect(screen.queryByRole("button", { name: "暂存并离开" })).toBeNull(); expect(screen.queryByRole("button", { name: "暂存" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "不暂存并离开" })); expect(closed).toHaveBeenCalledTimes(1); expect(await store.listPageDrafts("draft-teacher")).toEqual([]);
});

it("restores after server hydration without mistaking initial defaults for user edits", async () => {
  const loaded = await store.readPageDraft("draft-teacher", "hydrate", store.objectDraftCodec());
  await store.writePageDrafts([store.preparePageDraft("draft-teacher", "hydrate", "1", { name: "explicit", file: null }, store.objectDraftCodec(), loaded.epoch, null)]);
  function Hydrated() {
    const [value, setValue] = useState({ name: "", file: null as File | null });
    useEffect(() => { setValue({ name: "server", file: null }); }, []);
    useDraftProtection({ scope: "hydrate", value, baseline: { name: "server", file: null as File | null }, version: "1", onRestore: setValue });
    return <input aria-label="hydrated" value={value.name} readOnly />;
  }
  render(<PageDraftSession ownerId="draft-teacher"><DraftLeaveProvider><Hydrated /><DraftActions /></DraftLeaveProvider></PageDraftSession>);
  await waitFor(() => expect(screen.getByLabelText("hydrated")).toHaveValue("explicit"));
  expect(screen.queryByText(/发现已暂存草稿，但/)).toBeNull();
});
