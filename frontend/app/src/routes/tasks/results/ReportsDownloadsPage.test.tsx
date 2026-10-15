import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ResultArtifactVersion, TaskFinalizationResponse } from "@/types";
import type { Locale } from "@/i18n/messages";
import { ReportsDownloadsPage } from "./ReportsDownloadsPage";

vi.mock("@/api/tasks", () => ({ getTaskResultArtifacts: vi.fn(), generateTaskResultArtifacts: vi.fn(), getTaskResultArtifactBlob: vi.fn(), deleteTaskResultArtifact: vi.fn() }));
const api = await import("@/api/tasks");
const finalization = { task_id: "T1", task_status: "graded", workflow_revision: 7, final_result_version: 0,
  final_result_dirty: false, ready_for_confirmation: true, analysis_status: "not_generated" } as TaskFinalizationResponse;

function show(state = finalization, locale: Locale = "en-US") {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter><ReportsDownloadsPage locale={locale} taskId="T1" taskName="Example" finalization={state} /></MemoryRouter>
  </QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  const artifacts = { task_id: "T1", current_result_version: 0, analysis_status: "not_generated" as const, versions: [] };
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue(artifacts);
  vi.mocked(api.generateTaskResultArtifacts).mockResolvedValue({ ...finalization, status: "ok", unchanged: false, artifacts });
});

afterEach(() => vi.restoreAllMocks());

const report: ResultArtifactVersion = { version: 1, current: true, status: "ready", generated_at: 1_700_000_000,
  bundle_filename: "代数作业_2026-10-15_v1_reports.zip",
  files: [{ artifact_id: "grades_csv", title: "成绩表", title_en: "Grades", filename: "代数作业_2026-10-15_v1_grades.csv", media_type: "text/csv", size_bytes: 12, sha256: "example" }] };

it.each((["zh-CN", "en-US"] as const).flatMap(locale => [true, false].map(dirty => ({ locale, dirty }))))(
"keeps a current-format report downloadable when the server marks its data stale ($locale, dirty=$dirty)", async ({ locale, dirty }) => {
  const zh = locale === "zh-CN";
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue({ task_id: "T1", current_result_version: 1, analysis_status: "stale",
    versions: [{ ...report, status: "stale" }] });
  vi.mocked(api.getTaskResultArtifactBlob).mockResolvedValue(new Blob(["saved report"]));
  const confirm = vi.spyOn(window, "confirm");
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:report") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  const filenames: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { filenames.push(this.download); });
  show({ ...finalization, final_result_version: 1, final_result_dirty: dirty }, locale);
  expect(await screen.findByRole("heading", { name: zh ? "最新报告" : "Latest report" })).toBeVisible();
  expect(screen.getByRole("heading", { name: zh ? /^历史报告/ : /^Historical reports/ })).toBeVisible();
  expect(screen.getByText(zh ? "最新报告尚未生成，请先生成再下载。" : "No latest report yet. Generate it before downloading.")).toBeVisible();
  expect(screen.queryByRole("button", { name: zh ? "下载全部 ZIP" : "Download all ZIP" })).not.toBeInTheDocument();
  expect(screen.queryByText(/过期|out of date|stale|regeneration needed/i)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: zh ? "下载 ZIP" : "Download ZIP" }));
  await waitFor(() => expect(api.getTaskResultArtifactBlob).toHaveBeenCalledWith("T1", 1, "bundle"));
  expect(filenames).toEqual([report.bundle_filename]);
  expect(confirm).not.toHaveBeenCalled();
});

it("shows a generated latest report with time and an empty history", async () => {
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue({ task_id: "T1", current_result_version: 1, analysis_status: "ready", versions: [report] });
  show({ ...finalization, final_result_version: 1 });
  expect(await screen.findByRole("button", { name: "Download all ZIP" })).toBeVisible();
  expect(screen.getByText("Generated at")).toBeVisible();
  expect(screen.getByText("No historical reports.")).toBeVisible();
  expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
});

it("uses the server's readable names for the latest ZIP and single-file downloads", async () => {
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue({ task_id: "T1", current_result_version: 1, analysis_status: "ready", versions: [report] });
  vi.mocked(api.getTaskResultArtifactBlob).mockResolvedValue(new Blob(["report"]));
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:report") });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  const filenames: string[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { filenames.push(this.download); });
  show({ ...finalization, final_result_version: 1 });
  await userEvent.click(await screen.findByRole("button", { name: "Download all ZIP" }));
  await waitFor(() => expect(filenames).toEqual([report.bundle_filename]));
  await userEvent.click(screen.getByRole("button", { name: "Download" }));
  await waitFor(() => expect(filenames).toEqual([report.bundle_filename, report.files[0].filename]));
});

it("deletes only the selected historical version and refreshes its list", async () => {
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue({ task_id: "T1", current_result_version: 2, analysis_status: "ready",
    versions: [{ ...report, version: 2 }, { ...report, current: false, status: "historical" }] });
  vi.mocked(api.deleteTaskResultArtifact).mockResolvedValue({ task_id: "T1", current_result_version: 2, analysis_status: "ready", versions: [{ ...report, version: 2 }] });
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  show({ ...finalization, final_result_version: 2 });
  const remove = await screen.findByRole("button", { name: "Delete" });
  await userEvent.click(remove);
  expect(api.deleteTaskResultArtifact).not.toHaveBeenCalled();
  confirm.mockReturnValue(true);
  await userEvent.click(remove);
  await screen.findByText("No historical reports.");
  expect(api.deleteTaskResultArtifact).toHaveBeenCalledWith("T1", 1);
  expect(screen.getByRole("button", { name: "Download all ZIP" })).toBeVisible();
});

it.each(["zh-CN", "en-US"] as const)("hides paused JSON downloads and excludes them from current and historical counts (%s)", async (locale) => {
  const json = { artifact_id: "formal_result_json" as const, title: "正式结果数据", title_en: "Formal result data",
    filename: "result.json", media_type: "application/json", size_bytes: 100, sha256: "json" };
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue({ task_id: "T1", current_result_version: 2, analysis_status: "ready",
    versions: [{ ...report, version: 2, files: [...report.files, json] },
      { ...report, current: false, status: "historical", files: [...report.files, json] }] });
  show({ ...finalization, final_result_version: 2 }, locale);
  const zh = locale === "zh-CN";
  expect(await screen.findByRole("button", { name: zh ? "下载全部 ZIP" : "Download all ZIP" })).toBeVisible();
  expect(screen.queryByText(/JSON|正式结果数据|Formal result data/)).not.toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: zh ? "下载" : "Download" })).toHaveLength(1);
  expect(screen.getByText(zh ? "可下载文件" : "Downloadable files").parentElement).toHaveTextContent("1");
  expect(screen.getByText(zh ? "1 个文件可下载" : "1 files available")).toBeVisible();
});

it("only sends one atomic generation request after an explicit click", async () => {
  show();
  const button = await screen.findByRole("button", { name: "Generate files" });
  expect(api.generateTaskResultArtifacts).not.toHaveBeenCalled();
  await userEvent.click(button);
  await waitFor(() => expect(api.generateTaskResultArtifacts).toHaveBeenCalledWith("T1", 7));
});

it("offers generation with no downloads when retired reports are excluded", async () => {
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue({ task_id: "T1", current_result_version: 1, analysis_status: "not_generated",
    versions: [{ version: 1, current: true, status: "not_generated", files: [] }] });
  show({ ...finalization, final_result_version: 1 });
  await userEvent.click(await screen.findByRole("button", { name: "Generate files" }));
  await waitFor(() => expect(api.generateTaskResultArtifacts).toHaveBeenCalledWith("T1", 7));
  expect(screen.getByText("No historical reports.")).toBeVisible();
  expect(screen.queryByRole("button", { name: /Download/ })).not.toBeInTheDocument();
});

it("shows a recoverable error when atomic report generation conflicts", async () => {
  vi.mocked(api.generateTaskResultArtifacts).mockRejectedValue(new Error("revision_conflict"));
  show();
  await userEvent.click(await screen.findByRole("button", { name: "Generate files" }));
  await screen.findByRole("button", { name: "Generate again" });
  expect(api.generateTaskResultArtifacts).toHaveBeenCalledTimes(1);
});

it("allows explicit regeneration after grades change, without treating it as pending teacher review", async () => {
  vi.mocked(api.getTaskResultArtifacts).mockResolvedValue({ task_id: "T1", current_result_version: 1, analysis_status: "stale",
    versions: [{ version: 1, current: true, status: "stale", files: [] }] });
  show({ ...finalization, final_result_version: 1, final_result_dirty: true });
  await userEvent.click(await screen.findByRole("button", { name: "Generate files" }));
  await waitFor(() => expect(api.generateTaskResultArtifacts).toHaveBeenCalledWith("T1", 7));
  expect(screen.queryByText("Return to review")).not.toBeInTheDocument();
});
