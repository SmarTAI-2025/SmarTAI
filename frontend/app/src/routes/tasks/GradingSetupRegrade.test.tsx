import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GradingSetupPage } from "./GradingSetupPage";

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

describe("GradingSetupPage regrade mode", () => {
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
      mutateAsync: vi.fn(),
    });
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
});
