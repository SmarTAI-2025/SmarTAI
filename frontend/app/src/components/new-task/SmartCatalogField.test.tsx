import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { SmartCatalogField } from "./SmartCatalogField";
import { APIError } from "@/api/client";
import { userFacingError } from "@/lib/taskActionGuards";

const copy = vi.hoisted(() => ({ locale: "zh-CN" }));
vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({ locale: copy.locale, t: (key: string) => key }),
}));

interface TestItem {
  id: string;
  name: string;
}

it.each(["zh-CN", "en-US"])("localizes catalog creation failures without exposing raw errors (%s)", async locale => {
  copy.locale = locale;
  const error = new APIError(0, "Network Error");
  const onCreate = vi.fn().mockRejectedValue(error);
  const { input } = renderField("New course", vi.fn(), onCreate);
  fireEvent.focus(input);
  fireEvent.click(screen.getByRole("option", { name: /newTaskCreateCourse/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent(userFacingError(error, locale));
  expect(screen.getByRole("alert")).not.toHaveTextContent("Network Error");
  expect(onCreate).toHaveBeenCalledOnce();
  expect(input).toHaveValue("New course");
  copy.locale = "zh-CN";
});

function renderField(query: string, onQueryChange = vi.fn(), onCreate = vi.fn()) {
  render(
    <SmartCatalogField<TestItem>
      label="Course"
      hint="Search courses"
      placeholder="Search"
      resource="course"
      query={query}
      onQueryChange={onQueryChange}
      selected={[]}
      initialItems={[]}
      searchCandidates={[]}
      isSearching={false}
      isCreating={false}
      getId={(item) => item.id}
      getLabel={(item) => item.name}
      onSelect={vi.fn()}
      onRemove={vi.fn()}
      onCreate={onCreate}
    />,
  );
  return { input: screen.getByRole("combobox") as HTMLInputElement, onQueryChange };
}

function renderControlledField(initialQuery: string, onQueryChange = vi.fn()) {
  function ControlledField() {
    const [query, setQuery] = useState(initialQuery);
    return (
      <SmartCatalogField<TestItem>
        label="Course"
        hint="Search courses"
        placeholder="Search"
        resource="course"
        query={query}
        onQueryChange={(value) => {
          onQueryChange(value);
          setQuery(value);
        }}
        selected={[]}
        initialItems={[]}
        searchCandidates={[]}
        isSearching={false}
        isCreating={false}
        getId={(item) => item.id}
        getLabel={(item) => item.name}
        onSelect={vi.fn()}
        onRemove={vi.fn()}
        onCreate={vi.fn()}
      />
    );
  }

  render(<ControlledField />);
  return { input: screen.getByRole("combobox") as HTMLInputElement, onQueryChange };
}

describe("SmartCatalogField IME input", () => {
  it("keeps composition text local until the IME commits it", async () => {
    const { input, onQueryChange } = renderField("");

    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "s" } });
    fireEvent.change(input, { target: { value: "sa" } });
    fireEvent.change(input, { target: { value: "san" } });
    fireEvent.change(input, { target: { value: "三" } });

    expect(input).toHaveValue("三");
    expect(onQueryChange).not.toHaveBeenCalled();

    fireEvent.compositionEnd(input);
    fireEvent.change(input, { target: { value: "三" } });

    await waitFor(() => {
      expect(onQueryChange).toHaveBeenCalledTimes(1);
      expect(onQueryChange).toHaveBeenLastCalledWith("三");
    });
  });

  it("flushes the final composed value synchronously when the field loses focus", () => {
    const { input, onQueryChange } = renderField("");

    fireEvent.compositionStart(input);
    fireEvent.change(input, { target: { value: "san" } });
    fireEvent.compositionEnd(input);
    fireEvent.change(input, { target: { value: "三" } });
    fireEvent.blur(input);

    expect(onQueryChange).toHaveBeenCalledTimes(1);
    expect(onQueryChange).toHaveBeenLastCalledWith("三");
  });

  it("keeps the caret before the Chinese character across repeated deletions", async () => {
    const user = userEvent.setup();
    const { input, onQueryChange } = renderControlledField("ssasan三");
    await user.click(input);
    input.setSelectionRange(6, 6);

    const edits = [
      ["ssasa三", 5],
      ["ssas三", 4],
      ["ssa三", 3],
      ["ss三", 2],
      ["s三", 1],
      ["三", 0],
    ] as const;

    for (const [value, caret] of edits) {
      await user.keyboard("{Backspace}");
      await waitFor(() => expect(input).toHaveValue(value));
      expect(onQueryChange).toHaveBeenLastCalledWith(value);
      expect(input.selectionStart).toBe(caret);
      expect(input.selectionEnd).toBe(caret);
      expect(input).toHaveFocus();
    }
  });
});
