import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { getHealthStatus } from "@/api/health";
import { RecoverableActionState } from "@/components/ui/RecoverableActionState";
import { classifyRecoverableError } from "@/lib/taskActionGuards";
import { WorkflowTimeoutsProvider } from "./WorkflowTimeouts";

vi.mock("@/api/health", () => ({ getHealthStatus: vi.fn() }));

describe("workflow timeout notices", () => {
  it("shows each configured limit from the public backend response", async () => {
    vi.mocked(getHealthStatus).mockResolvedValue({
      status: "healthy", engine: "v2", memory_usage_mb: 1, cpu_percent: 0,
      workflow_timeout_minutes: {
        question_preparation: 41, submission_recognition: 62, grading: 83, knowledge_ingestion: 27,
      },
    });
    const codes = [
      "question_preparation_wall_time_limit_reached", "submission_wall_time_limit_reached",
      "grading_wall_time_limit_reached", "knowledge_wall_time_limit_reached",
    ];
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MemoryRouter>
          <WorkflowTimeoutsProvider>
            {codes.map((code) => <RecoverableActionState key={code} info={classifyRecoverableError(code)} />)}
          </WorkflowTimeoutsProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    for (const minutes of [41, 62, 83, 27]) {
      expect(await screen.findByText(new RegExp(`当前配置为 ${minutes} 分钟`))).toBeVisible();
    }
    expect(getHealthStatus).toHaveBeenCalledTimes(1);
  });
});
