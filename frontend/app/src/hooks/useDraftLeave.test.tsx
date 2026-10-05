import "fake-indexeddb/auto";
import { Blob, File } from "node:buffer";
import { useEffect, useState } from "react";
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, Link, Outlet, RouterProvider } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { DraftActions, DraftLeaveProvider } from "./useDraftLeave";
import { PageDraftSession, useDraftProtection } from "./useDraftProtection";
import * as store from "@/lib/pageDraftStore";

let version = "1";
let busy = false;
let secret = false;
const closed = vi.fn();
const formalSubmit = vi.fn(async () => {});
function Form() {
  const [value, setValue] = useState({ name: "server", file: null as File | null });
  const draft = useDraftProtection({ scope: "test:task:field", value, baseline: { name: "server", file: null as File | null }, version, busy, secret, onRestore: setValue });
  return <><input aria-label="name" value={value.name} onChange={e => setValue({ ...value, name: e.target.value })} /><Link to="/next">next</Link><button onClick={() => draft.requestLeave(closed)}>close editor</button><button onClick={() => void draft.clear()}>formal success</button><button onClick={() => void draft.runFormal(formalSubmit, value)}>run formal</button><button onClick={() => { busy = !busy; setValue({ ...value }); }}>toggle busy</button><button onClick={() => { version = "2"; setValue({ ...value }); }}>server changes</button></>;
}
function setup(owner = "draft-teacher") {
  const router = createMemoryRouter([{ element: <DraftLeaveProvider><Outlet /><DraftActions /></DraftLeaveProvider>, children: [{ path: "/", element: <Form /> }, { path: "/next", element: <p>destination</p> }] }], { initialEntries: ["/"] });
  return { ...render(<PageDraftSession ownerId={owner}><RouterProvider router={router} /></PageDraftSession>), router };
}
async function ready() { await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled()); }
function type(name: string) { fireEvent.change(screen.getByLabelText("name"), { target: { value: name } }); }
async function save() { await ready(); fireEvent.click(screen.getByRole("button", { name: "暂存" })); await screen.findByText(/已暂存 ·/); }
beforeEach(async () => { vi.restoreAllMocks(); vi.stubGlobal("Blob", Blob); vi.stubGlobal("File", File); await store.clearPageDrafts(); version = "1"; busy = false; secret = false; closed.mockReset(); formalSubmit.mockClear(); });

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
it("can explicitly keep current input after a server update and save without submitting", async () => {
  setup(); await ready(); type("old local"); await save(); type("new input");
  fireEvent.click(screen.getByText("server changes"));
  fireEvent.click(await screen.findByRole("button", { name: "保留当前输入" }));
  expect(screen.getByLabelText("name")).toHaveValue("new input"); await save();
  expect((await store.readPageDraft("draft-teacher", "test:task:field", store.objectDraftCodec())).value).toMatchObject({ name: "new input" });
  expect(formalSubmit).not.toHaveBeenCalled();
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


it("a completed formal save resumes its pending intent once; a new busy operation is protected", async () => {
  setup(); await ready(); type("business");
  fireEvent.click(screen.getByText("toggle busy"));
  fireEvent.click(screen.getByText("close editor"));
  await screen.findByRole("alertdialog");
  fireEvent.click(screen.getByText("formal success"));
  fireEvent.click(screen.getByText("toggle busy"));
  await waitFor(() => expect(closed).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole("alertdialog")).toBeNull();
  fireEvent.click(screen.getByText("toggle busy"));
  const event = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
});

it("new credential input remains protected after session draft cleanup", async () => {
  secret = true; setup(); type("first secret");
  await act(async () => { await store.clearPageDrafts(); });
  type("new secret"); fireEvent.click(screen.getByText("close editor"));
  await screen.findByRole("alertdialog");
  expect(screen.queryByRole("button", { name: "暂存并离开" })).toBeNull();
  expect(await store.listPageDrafts("draft-teacher")).toEqual([]);
});


it("formal API and asynchronous cleanup keep one lock and preserve edits made while saving", async () => {
  setup(); await ready(); type("submitted"); await save();
  const remove = store.removePageDraft; let release!: () => void;
  vi.spyOn(store, "removePageDraft").mockImplementationOnce(async (...args) => { await new Promise<void>(resolve => { release = resolve; }); await remove(...args); });
  fireEvent.click(screen.getByText("run formal"));
  await waitFor(() => expect(release).toBeTypeOf("function"));
  fireEvent.click(screen.getByText("run formal")); expect(formalSubmit).toHaveBeenCalledTimes(1);
  const unloading = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(unloading); expect(unloading.defaultPrevented).toBe(true);
  type("later input"); fireEvent.click(screen.getByText("close editor")); await screen.findByRole("alertdialog");
  await act(async () => release());
  expect(closed).not.toHaveBeenCalled(); expect(screen.getByLabelText("name")).toHaveValue("later input");
  fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
  expect(await store.listPageDrafts("draft-teacher")).toEqual([]);
});


it("publishes a durable save to navigation guards before React rerenders", async () => {
  const value = { name: "saved input" };
  const { result } = renderHook(() => useDraftProtection({
    scope: "save-navigation", value, version: "1", onRestore: () => {},
  }), { wrapper: ({ children }) => <PageDraftSession ownerId="draft-teacher"><DraftLeaveProvider>{children}</DraftLeaveProvider></PageDraftSession> });
  await waitFor(() => expect(result.current.controller().loaded).toBe(true));
  const prepared = result.current.controller().prepare();
  await act(async () => {
    await store.writePageDrafts([prepared.write]);
    prepared.commit();
    // The caller continues synchronously, before React flushes setSavedAt.
    expect(result.current.controller().savedAt).toBe(prepared.write.record.savedAt);
    expect(result.current.controller().dirty).toBe(false);
  });
});


it("beforeunload reads the committed draft before React flushes the save render", async () => {
  const value = { name: "unsaved input" };
  const { result } = renderHook(() => useDraftProtection({
    scope: "unload-save-boundary", value, baseline: { name: "server" }, version: "1", onRestore: () => {},
  }), { wrapper: ({ children }) => <PageDraftSession ownerId="draft-teacher"><DraftLeaveProvider>{children}</DraftLeaveProvider></PageDraftSession> });
  await waitFor(() => expect(result.current.controller().loaded).toBe(true));
  const before = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(before);
  expect(before.defaultPrevented).toBe(true);
  const prepared = result.current.controller().prepare();
  await act(async () => {
    await store.writePageDrafts([prepared.write]);
    prepared.commit();
    const after = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(after);
    expect(after.defaultPrevented).toBe(false);
  });
});

it("a canceled router blocker is not resumed by later formal-save completion", async () => {
  const { router } = setup(); await ready(); type("business");
  fireEvent.click(screen.getByText("next"));
  await screen.findByRole("alertdialog");
  const blocked = [...router.state.blockers.values()].find(item => item.state === "blocked");
  expect(blocked?.state).toBe("blocked");
  await act(async () => {
    // Router cancellation and the business completion arrive before effects flush.
    if (blocked?.state === "blocked") blocked.reset();
    fireEvent.click(screen.getByText("run formal"));
  });
  await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  expect(router.state.errors).toBeNull();
  expect(router.state.location.pathname).toBe("/");
  expect(screen.getByLabelText("name")).toHaveValue("business");
  expect(screen.queryByRole("alertdialog")).toBeNull();
});

it("repeated blocked route clicks keep the first destination and resume only once", async () => {
  const { router } = setup(); await ready(); type("business");
  fireEvent.click(screen.getByText("next"));
  await screen.findByRole("alertdialog");
  await act(async () => { await router.navigate("/next?later=1"); });
  fireEvent.click(screen.getByRole("button", { name: "暂存并离开" }));
  await screen.findByText("destination");
  expect(router.state.errors).toBeNull();
  expect(router.state.location.search).toBe("");
});
