import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { expect, it, vi } from "vitest";
import { useUploadKBDoc } from "./kb";
import { gradingSetupKeys } from "./keys";

vi.mock("@/api/kb", () => ({ addKBDoc: vi.fn(async () => ({ workflow_revision: 2 })) }));

it("waits for the new grading revision before reporting an attachment as finished", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(gradingSetupKeys.detail("task"), { workflow_revision: 1 });
  let release!: (data: { workflow_revision: number }) => void;
  const refetch = vi.fn(() => new Promise<{ workflow_revision: number }>(resolve => { release = resolve; }));
  const { result } = renderHook(() => {
    const setup = useQuery({ queryKey: gradingSetupKeys.detail("task"), queryFn: refetch });
    return { upload: useUploadKBDoc(), setup };
  }, { wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider> });
  let finished = false;
  let upload!: Promise<unknown>;
  act(() => { upload = result.current.upload.mutateAsync({ taskId: "task", libraryMaterialId: "material" }).then(() => { finished = true; }); });
  await waitFor(() => expect(refetch).toHaveBeenCalled());
  expect(finished).toBe(false);
  await act(async () => { release({ workflow_revision: 2 }); await upload; });
  expect(client.getQueryData<{ workflow_revision: number }>(gradingSetupKeys.detail("task"))?.workflow_revision).toBe(2);
  await waitFor(() => expect(result.current.setup.data?.workflow_revision).toBe(2));
  expect(finished).toBe(true);
});
