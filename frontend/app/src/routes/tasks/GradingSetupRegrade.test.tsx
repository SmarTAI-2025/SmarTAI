import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GradingSetupPage } from "./GradingSetupPage";
import { DraftActions, DraftLeaveProvider } from "@/hooks/useDraftLeave";
import { PageDraftSession } from "@/hooks/useDraftProtection";
import { clearPageDrafts } from "@/lib/pageDraftStore";

vi.mock("@/api/hooks/tasks", () => ({ useTask: () => ({ data: undefined }) }));

vi.mock("@/api/hooks", () => ({
  useCourseMaterials: vi.fn(),
  useDeleteKBDoc: vi.fn(),
  useGradingSetup: vi.fn(),
  useKBDocs: vi.fn(),
  useSaveGradingSetup: vi.fn(),
  useUploadKBDoc: vi.fn(),
}));

vi.mock("@/components/new-task/NewTaskStepper", () => ({
  NewTaskStepper: () => <div>Workflow steps</div>,
}));

vi.mock("@/i18n/I18nProvider", () => ({
  useI18n: () => ({
    locale: "en-US",
    t: (key: string) => key,
  }),
}));

const {
  useCourseMaterials,
  useDeleteKBDoc,
  useGradingSetup,
  useKBDocs,
  useSaveGradingSetup,
  useUploadKBDoc,
} = await import("@/api/hooks");

const saveSetupMutate = vi.fn();

describe("GradingSetupPage regrade mode", () => {
  it.each([0.3, 0.8])("normalizes historical threshold %s to the fixed policy without a confidence slider", async threshold => {
    await clearPageDrafts();
    const current = (useGradingSetup as unknown as () => any)();
    current.data.grading_setup.low_confidence_threshold = threshold;
    const router = createMemoryRouter([
      { path: "/tasks/:taskId/grading-setup", element: <GradingSetupPage /> },
      { path: "/tasks/:taskId/grading/preflight", element: <div>New summary</div> },
    ], { initialEntries: ["/tasks/task-1/grading-setup"] });
    render(<RouterProvider router={router} />);
    fireEvent.click(await screen.findByRole("button", { name: "Show advanced settings" }));
    expect(screen.getAllByRole("slider")).toHaveLength(1);
    expect(screen.getByText(/Any valid expert confidence below 0.60/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Save & Review/ }));
    await waitFor(() => expect(saveSetupMutate).toHaveBeenCalledWith(expect.objectContaining({
      gradingSetup: expect.objectContaining({ low_confidence_threshold: 0.6 }),
    })));
  });

  it.each(["error", "graded", "finalized"])("saves %s settings into a new summary even with a historical return address", async (status) => {
    await clearPageDrafts();
    const current = (useGradingSetup as unknown as () => any)();
    current.data.task_status = status;
    current.data.readiness = { ready: true, blocking_issues: [], warnings: [] };
    const router = createMemoryRouter([
      { path: "/tasks/:taskId/grading-setup", element: <GradingSetupPage /> },
      { path: "/tasks/:taskId/grading/preflight", element: <div>New summary</div> },
      { path: "/tasks/:taskId/grading/progress", element: <div>Old stopped run</div> },
    ], { initialEntries: ["/tasks/task-1/grading-setup?returnTo=%2Ftasks%2Ftask-1%2Fgrading%2Fprogress"] });
    render(<RouterProvider router={router} />);
    fireEvent.click(await screen.findByRole("button", { name: /Save & Review/ }));
    expect(await screen.findByText("New summary")).toBeVisible();
    expect(saveSetupMutate).toHaveBeenCalledTimes(1);
    expect(router.state.errors).toBeNull();
  });
  beforeEach(() => {
    vi.clearAllMocks();
    (useGradingSetup as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: {
        task_id: "task-1",
        task_status: "graded",
        workflow_revision: 7,
        configured: true,
        grading_setup: {
          schema_version: 1,
          selected_provider_ids: ["provider-1"],
          primary_provider_id: "provider-1",
          aggregation_method: "single",
          multi_sample_n: 1,
          knowledge_scope: "none",
          strictness: 50,
          allow_partial_credit: true,
          feedback_tone: "neutral",
          feedback_length: "medium",
          feedback_language: "en",
          suggest_corrections: true,
          low_confidence_threshold: 0.6,
          teacher_notes: "",
        },
        suggested_setup: null,
        grading_setup_fingerprint: "setup-1",
        grading_setup_updated_at: 1,
        available_experts: [{
          provider_id: "provider-1",
          provider_type: "gemini",
          model: "gemini-3.1-flash-lite-preview",
          display_name: "Calculus grader",
          enabled: true,
          scope: "owner",
          is_shared: false,
          editable: true,
          max_concurrent: 1,
          rpm: 10,
        }],
        knowledge: {
          scope_options: ["none", "all_task_docs"],
          task_doc_count: 0,
          task_docs: [],
        },
        readiness: {
          ready: false,
          blocking_issues: ["invalid_state"],
          warnings: [],
        },
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    (useSaveGradingSetup as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      isPending: false,
      mutateAsync: saveSetupMutate,
    });
    saveSetupMutate.mockImplementation(async ({ gradingSetup }) => ({
      ...(useGradingSetup as unknown as () => any)().data,
      grading_setup: gradingSetup,
    }));
    (useKBDocs as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { docs: [] },
      isLoading: false,
      isSuccess: true,
    });
    (useCourseMaterials as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { items: [] },
      isLoading: false,
    });
    (useUploadKBDoc as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      isPending: false,
      mutateAsync: vi.fn(),
    });
    (useDeleteKBDoc as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      isPending: false,
      mutateAsync: vi.fn(),
    });
  });

  it("lets knowledge-only revisions be drafted while protecting actual grading-setting changes", async () => {
    await clearPageDrafts();
    const current = (useGradingSetup as unknown as () => any)();
    const router = createMemoryRouter([{ path: "/tasks/:taskId/grading-setup", element:
      <PageDraftSession ownerId="revision-test"><DraftLeaveProvider><GradingSetupPage /><DraftActions /></DraftLeaveProvider></PageDraftSession>,
    }], { initialEntries: ["/tasks/task-1/grading-setup"] });
    render(<RouterProvider router={router} />);
    const slider = await screen.findByRole("slider");
    await waitFor(() => expect(screen.getByRole("button", { name: "暂存" })).toBeEnabled());
    fireEvent.change(slider, { target: { value: "60" } });
    current.data = { ...current.data, workflow_revision: 8, knowledge: { ...current.data.knowledge, task_doc_count: 1 } };
    fireEvent.change(slider, { target: { value: "70" } });
    expect(screen.queryByText(/服务器业务版本已变化/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "暂存" }));
    await screen.findByText(/已暂存 ·/);
    expect(slider).toHaveValue("70"); expect(saveSetupMutate).not.toHaveBeenCalled();
    current.data = { ...current.data, workflow_revision: 9, grading_setup_fingerprint: "setup-2", grading_setup: { ...current.data.grading_setup, strictness: 20 } };
    fireEvent.change(slider, { target: { value: "80" } });
    await screen.findByText(/服务器业务版本已变化/);
    expect(slider).toHaveValue("80");
  });

  it("defaults to confidence weights and saves custom ratios for the selected models", async () => {
    await clearPageDrafts();
    const current = (useGradingSetup as unknown as () => any)();
    current.data.task_status = "submissions_ready";
    current.data.grading_setup = { ...current.data.grading_setup, selected_provider_ids: ["provider-1", "provider-2"], aggregation_method: "weighted_average" };
    current.data.available_experts.push({ ...current.data.available_experts[0], provider_id: "provider-2", display_name: "Second grader" });
    const router = createMemoryRouter([
      { path: "/tasks/:taskId/grading-setup", element: <GradingSetupPage /> },
      { path: "/tasks/:taskId/grading/preflight", element: <div>Saved weight summary</div> },
    ], { initialEntries: ["/tasks/task-1/grading-setup"] });
    render(<RouterProvider router={router} />);
    expect(await screen.findByText("Default: confidence weights")).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "Custom model weights" })).not.toBeChecked();
    fireEvent.click(screen.getByRole("checkbox", { name: "Custom model weights" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "Calculus grader Weight" }), { target: { value: "1" } });
    fireEvent.change(screen.getByRole("spinbutton", { name: "Second grader Weight" }), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Save & Review Grading" }));
    await waitFor(() => expect(saveSetupMutate).toHaveBeenCalledWith(expect.objectContaining({ gradingSetup: expect.objectContaining({ expert_weights: { "provider-1": 1, "provider-2": 3 } }) })));
    expect(await screen.findByText("Saved weight summary")).toBeVisible();
  });

  it("keeps completed-task settings editable and offers the regrade summary action", async () => {
    const router = createMemoryRouter([
      {
        path: "/tasks/:taskId/grading-setup",
        element: <GradingSetupPage />,
      },
    ], {
      initialEntries: ["/tasks/task-1/grading-setup"],
    });

    render(<RouterProvider router={router} />);

    expect(await screen.findByText(/You are preparing a regrade/)).toBeInTheDocument();
    expect(screen.getByRole("slider")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Save & Review Regrade" })).toBeEnabled();
    expect(screen.queryByText(/not ready for grading setup/i)).not.toBeInTheDocument();
  });

  it("keeps a selected OCR record visible while warning the teacher to change it", async () => {
    const current = (useGradingSetup as unknown as () => any)();
    (useGradingSetup as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...current,
      data: {
        ...current.data,
        grading_setup: {
          ...current.data.grading_setup,
          selected_provider_ids: ["ocr:baidu_unlimited_ocr:record-1"],
          primary_provider_id: "ocr:baidu_unlimited_ocr:record-1",
        },
        available_experts: [{
          provider_id: "ocr:baidu_unlimited_ocr:record-1",
          provider_type: "baidu_unlimited_ocr",
          provider_kind: "ocr",
          credential_id: "record-1",
          model: "unlimited-ocr-parser",
          display_name: "Baidu Document Parsing (Unlimited-OCR)",
          enabled: true,
          scope: "owner",
          is_shared: false,
          editable: false,
          max_concurrent: 1,
          rpm: 0,
        }],
      },
    });
    const router = createMemoryRouter([
      {
        path: "/tasks/:taskId/grading-setup",
        element: <GradingSetupPage />,
      },
    ], {
      initialEntries: ["/tasks/task-1/grading-setup"],
    });

    render(<RouterProvider router={router} />);

    expect((await screen.findAllByText(
      "This OCR service does not support grading. Choose a grading model.",
    ))[0]).toBeInTheDocument();
    expect(screen.getByText("Baidu Document Parsing (Unlimited-OCR)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save & Review Regrade" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Save & Review Regrade" }));
    expect(saveSetupMutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Fix settings" }));
    await waitFor(() => expect(document.getElementById("grading-model-selection")).toHaveFocus());
    expect(screen.getByRole("link", { name: "Open Models & BYOK" })).toHaveAttribute("href", "/settings/byok?returnTo=%2Ftasks%2Ftask-1%2Fgrading-setup");
  });

  it("saves settings independently while a source blocker still prevents grading start", async () => {
    const current = (useGradingSetup as unknown as () => any)();
    (useGradingSetup as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...current,
      data: {
        ...current.data,
        task_status: "error",
        readiness: {
          ready: false,
          blocking_issues: ["submission_sources_failed"],
          warnings: [],
        },
      },
    });
    const router = createMemoryRouter([
      {
        path: "/tasks/:taskId/grading-setup",
        element: <GradingSetupPage />,
      },
      {
        path: "/tasks/:taskId/grading/preflight",
        element: <div>Grading preflight</div>,
      },
    ], {
      initialEntries: ["/tasks/task-1/grading-setup"],
    });

    render(<RouterProvider router={router} />);

    const saveButton = await screen.findByRole("button", { name: "Save & Review Grading" });
    expect(saveButton).toBeEnabled();
    expect(screen.getByText(/Grading cannot start yet, but you can still save/)).toBeInTheDocument();

    fireEvent.click(saveButton);

    await waitFor(() => expect(saveSetupMutate).toHaveBeenCalledWith({
      taskId: "task-1",
      expectedWorkflowRevision: 7,
      gradingSetup: current.data.grading_setup,
    }));
    expect(await screen.findByText("Grading preflight")).toBeInTheDocument();
  });

  it.each([
    ["workflow_busy", "The task is busy with another operation. Save after it finishes."],
    ["grading_setup_locked", "Grading has started, so this setup is locked."],
  ])("does not save or advance setup while blocked by %s", async (blockingIssue, message) => {
    const current = (useGradingSetup as unknown as () => any)();
    (useGradingSetup as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      ...current,
      data: {
        ...current.data,
        task_status: "error",
        readiness: {
          ready: false,
          blocking_issues: [blockingIssue],
          warnings: [],
        },
      },
    });
    const router = createMemoryRouter([
      {
        path: "/tasks/:taskId/grading-setup",
        element: <GradingSetupPage />,
      },
    ], {
      initialEntries: ["/tasks/task-1/grading-setup"],
    });

    render(<RouterProvider router={router} />);

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.getByRole("slider")).toBeDisabled();
    const saveButton = screen.getByRole("button", { name: "Save & Review Grading" });
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);
    expect(saveSetupMutate).not.toHaveBeenCalled();
    expect(screen.getByRole("alertdialog")).toHaveTextContent(message);
    expect(screen.getByRole("button", { name: "View current progress" })).toBeInTheDocument();
  });

  it("guides an empty model selection back to the editable model choices", async () => {
    const current = (useGradingSetup as unknown as () => any)();
    current.data.grading_setup.selected_provider_ids = [];
    current.data.grading_setup.primary_provider_id = "";
    const router = createMemoryRouter([{ path: "/tasks/:taskId/grading-setup", element: <GradingSetupPage /> }], {
      initialEntries: ["/tasks/task-1/grading-setup"],
    });
    render(<RouterProvider router={router} />);
    const saveButton = await screen.findByRole("button", { name: "Save & Review Regrade" });
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Fix settings" }));
    await waitFor(() => expect(document.getElementById("grading-model-selection")).toHaveFocus());
    expect(saveSetupMutate).not.toHaveBeenCalled();
  });
});
import "fake-indexeddb/auto";

function render(ui: ReactElement) { return rtlRender(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>); }
