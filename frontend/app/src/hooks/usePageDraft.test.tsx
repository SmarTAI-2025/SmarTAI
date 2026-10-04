import "fake-indexeddb/auto";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it } from "vitest";
import { PageDraftSession, usePageDraft } from "./usePageDraft";
import { DraftActions, DraftLeaveProvider } from "./useDraftLeave";
import { clearPageDrafts, readPageDraft, type DraftCodec } from "@/lib/pageDraftStore";
const codec: DraftCodec<{ name: string }> = { encode: ({ name }) => ({ name }), decode: (value) => value && typeof value === "object" && "name" in value && typeof value.name === "string" ? { name: value.name } : null };
let lateWrite: () => void;
function Form({ scope }: { scope: string }) {
  const draft = usePageDraft(scope, () => ({ name: "" }), codec); const [name, setName] = draft.field("name");
  lateWrite = () => setName("late callback");
  return <><input aria-label="name" value={name} onChange={(e) => setName(e.target.value)} /><button onClick={draft.clear}>submit</button><button onClick={draft.reset}>reset</button><DraftActions /></>;
}
function App({ owner = "a", scope = "new" }: { owner?: string; scope?: string }) { return <DraftLeaveProvider><PageDraftSession ownerId={owner}><Form key={`${owner}:${scope}`} scope={scope} /></PageDraftSession></DraftLeaveProvider>; }
beforeEach(async () => { await clearPageDrafts(); });
async function save() { await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled()); fireEvent.click(screen.getByRole("button", { name: "暂存" })); await waitFor(() => expect(screen.getByText(/已暂存 ·/)).toBeInTheDocument()); }
it("only explicit save restores after remount; isolates users and tasks", async () => {
  let view = render(<App />); fireEvent.change(screen.getByLabelText("name"), { target: { value: "explicit" } }); await save();
  view.unmount(); view = render(<App />); await waitFor(() => expect(screen.getByLabelText("name")).toHaveValue("explicit"));
  fireEvent.change(screen.getByLabelText("name"), { target: { value: "never saved" } }); view.unmount(); view = render(<App />); await waitFor(() => expect(screen.getByLabelText("name")).toHaveValue("explicit"));
  view.rerender(<App scope="other" />); expect(screen.getByLabelText("name")).toHaveValue("");
  view.rerender(<App owner="b" />); expect(screen.getByLabelText("name")).toHaveValue("");
});
it("formal success removes the snapshot and seals late callbacks", async () => {
  const view = render(<App />); fireEvent.change(screen.getByLabelText("name"), { target: { value: "submit" } }); await save();
  fireEvent.click(screen.getByText("submit")); act(() => lateWrite()); view.unmount(); render(<App />);
  await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled()); expect(screen.getByLabelText("name")).toHaveValue("");
});
it("reset clears persisted work and allows a new explicit snapshot", async () => {
  let view = render(<App />); fireEvent.change(screen.getByLabelText("name"), { target: { value: "old" } }); await save(); fireEvent.click(screen.getByText("reset")); expect(screen.getByLabelText("name")).toHaveValue("");
  await waitFor(async () => expect((await readPageDraft("a", "new", codec)).record?.deleted).toBe(true)); fireEvent.change(screen.getByLabelText("name"), { target: { value: "fresh" } }); await save(); view.unmount(); view = render(<App />); await waitFor(() => expect(screen.getByLabelText("name")).toHaveValue("fresh"));
});
it("logout fences the old owner callbacks", async () => {
  const view = render(<App />); const oldWrite = lateWrite; await act(clearPageDrafts); view.unmount(); act(() => oldWrite()); render(<App />); expect(screen.getByLabelText("name")).toHaveValue("");
});
