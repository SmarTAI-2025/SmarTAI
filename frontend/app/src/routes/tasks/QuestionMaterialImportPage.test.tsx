import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QuestionMaterialImportPage } from "./QuestionMaterialImportPage";

const preflight = vi.hoisted(() => vi.fn());
const start = vi.hoisted(() => vi.fn());
vi.mock("@/api/hooks", () => ({
  useTask: () => ({ isSuccess: true, data: { task_id: "task-1", status: "problems_ready", course_id: "course-1" } }),
  useExperts: () => ({ isSuccess: true, data: [{ enabled: true }] }),
  usePreflightMaterialImport: () => ({ mutateAsync: preflight, isPending: false }),
  useStartMaterialImport: () => ({ mutateAsync: start, isPending: false }),
  useProblemSourceLibrary: () => ({ data: { items: [] } }),
}));
vi.mock("@/i18n/I18nProvider", () => ({ useI18n: () => ({ locale: "zh-CN" }) }));
vi.mock("@/hooks/useDraftProtection", () => ({ useDraftProtection: () => ({ isCurrent: () => true, clear: vi.fn(), conflict: null }) }));
vi.mock("@/hooks/useProblemDraftReferences", () => ({ useProblemDraftReferences: () => ({ blocked: false }) }));
vi.mock("@/components/new-task/NewTaskStepper", () => ({ NewTaskStepper: () => null }));

function openUpload() {
  const rendered = render(<MemoryRouter initialEntries={["/tasks/task-1/questions/import"]}><Routes><Route path="/tasks/:taskId/questions/import" element={<QuestionMaterialImportPage />} /></Routes></MemoryRouter>);
  fireEvent.click(screen.getByRole("button", { name: "上传新文件" }));
  return rendered.container.querySelector<HTMLInputElement>('input[type="file"]')!;
}

beforeEach(() => { vi.clearAllMocks(); vi.spyOn(window, "alert").mockImplementation(() => {}); });

describe("bulk material upload contracts", () => {
  it("updates JSON acceptance by target and preserves a now-incompatible selection without submitting", () => {
    const input = openUpload();
    expect(input.accept).not.toContain(".json");
    expect(screen.getByText(/单个文件最大 5 MiB/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "编程题测试样例" }));
    expect(input.accept).toContain(".json");
    fireEvent.change(input, { target: { files: [new File(["{}"], "cases.json")] } });
    expect(screen.getByText(/已选择：cases.json/)).toBeInTheDocument();
    expect(screen.getByLabelText("同时保存到课程资料库")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "编程题测试样例" }));
    expect(input.accept).not.toContain(".json");
    expect(screen.getByText(/已选择：cases.json/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("当前资料类型不支持");
    fireEvent.click(screen.getByRole("button", { name: "开始导入并匹配到题目" }));
    expect(preflight).not.toHaveBeenCalled();
    expect(start).not.toHaveBeenCalled();
  });
  it("rejects empty and oversized files before preflight", () => {
    const input = openUpload();
    fireEvent.change(input, { target: { files: [new File([], "empty.txt")] } });
    expect(screen.getByRole("alert")).toHaveTextContent("0 字节");
    const file = new File(["x"], "large.pdf");
    Object.defineProperty(file, "size", { value: 5 * 1024 * 1024 + 1 });
    fireEvent.change(input, { target: { files: [file] } });
    expect(screen.getByRole("alert")).toHaveTextContent("5 MiB");
    expect(preflight).not.toHaveBeenCalled();
  });
  it("allows images for every target and preserves the explicit save-to-library choice", () => {
    const input = openUpload();
    expect(input.accept).toContain(".png");
    fireEvent.click(screen.getByLabelText("同时保存到课程资料库"));
    fireEvent.change(input, { target: { files: [new File(["image"], "scan.png")] } });
    expect(screen.getByLabelText("同时保存到课程资料库")).not.toBeDisabled();
    expect(screen.getByLabelText("同时保存到课程资料库")).toBeChecked();
    expect(screen.getByText(/图片需视觉\/OCR 模型后台识别后才可检索/)).toBeInTheDocument();
  });
});
