import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it } from "vitest";
import { PageDraftSession, usePageDraft } from "./usePageDraft";
import { clearPageDrafts, type DraftCodec } from "@/lib/pageDraftStore";

const codec: DraftCodec<{ name: string }> = {
  encode: ({ name }) => ({ name }),
  decode: (value) => value && typeof value === "object" && "name" in value && typeof value.name === "string" ? { name: value.name } : null,
};
let lateWrite: () => void;
function Form({ scope }: { scope: string }) {
  const draft = usePageDraft(scope, () => ({ name: "" }), codec);
  const [name, setName] = draft.field("name");
  lateWrite = () => setName("late upload callback");
  return <><input aria-label="name" value={name} onChange={(e) => setName(e.target.value)} />
    <button onClick={draft.clear}>submit</button><button onClick={draft.reset}>discard</button></>;
}
function App({ owner = "a", scope = "new" }: { owner?: string; scope?: string }) {
  return <PageDraftSession ownerId={owner}><Form key={scope} scope={scope} /></PageDraftSession>;
}
beforeEach(clearPageDrafts);
it("restores on remount, isolates task navigation, and never carries state to a new owner", () => {
  const view = render(<App />);
  fireEvent.change(screen.getByLabelText("name"), { target: { value: "task-a" } });
  view.rerender(<App scope="other" />);
  expect(screen.getByLabelText("name")).toHaveValue("");
  view.rerender(<App />);
  expect(screen.getByLabelText("name")).toHaveValue("task-a");
  view.rerender(<App owner="b" />);
  expect(screen.getByLabelText("name")).toHaveValue("");
});
it("submission clears the draft and rejects late callbacks", () => {
  const view = render(<App />);
  fireEvent.change(screen.getByLabelText("name"), { target: { value: "submitted" } });
  fireEvent.click(screen.getByText("submit")); act(() => lateWrite());
  view.unmount(); render(<App />);
  expect(screen.getByLabelText("name")).toHaveValue("");
});
it("discard clears immediately and permits a fresh draft", () => {
  const view = render(<App />);
  fireEvent.change(screen.getByLabelText("name"), { target: { value: "discarded" } });
  fireEvent.click(screen.getByText("discard"));
  expect(screen.getByLabelText("name")).toHaveValue("");
  fireEvent.change(screen.getByLabelText("name"), { target: { value: "fresh" } });
  view.unmount(); render(<App />);
  expect(screen.getByLabelText("name")).toHaveValue("fresh");
});
it("logout fences old asynchronous callbacks", () => {
  const view = render(<App />); const oldWrite = lateWrite;
  act(clearPageDrafts); view.unmount(); act(() => oldWrite());
  render(<App />);
  expect(screen.getByLabelText("name")).toHaveValue("");
});
