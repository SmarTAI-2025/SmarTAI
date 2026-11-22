import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getJSON, postJSON } from "@/api/client";
import { taskKeys } from "@/api/hooks/keys";
import type { QuestionStemSave } from "@/types";

type StemSaveInput = {
  qId: string;
  stem: string;
  expectedRevision: number;
  retryOf?: string;
};

function inProgress(save: QuestionStemSave) {
  return save.status === "pending" || save.status === "running";
}

export function useQuestionStemSaves(taskId: string, revision: number | undefined, enabled: boolean) {
  const client = useQueryClient();
  const taskPath = `/tasks/${encodeURIComponent(taskId)}`;
  const queryKey = taskKeys.stemSaves(taskId);
  const saves = useQuery({
    queryKey,
    enabled,
    queryFn: async () => {
      const rows = await getJSON<QuestionStemSave[]>(`${taskPath}/question-stem-saves`);
      return Array.isArray(rows) ? rows : [];
    },
    refetchInterval: query => query.state.data?.some(
      save => inProgress(save) || (save.workflow_revision ?? 0) > (revision ?? 0),
    ) ? 1_500 : false,
  });
  const refreshRequired = saves.data?.some(save => (save.workflow_revision ?? 0) > (revision ?? 0));
  useEffect(() => {
    if (refreshRequired) void client.invalidateQueries({ queryKey: taskKeys.detail(taskId) });
  }, [client, refreshRequired, saves.dataUpdatedAt, taskId]);

  const submit = useMutation({
    mutationFn: ({ qId, stem, expectedRevision, retryOf }: StemSaveInput) =>
      postJSON<QuestionStemSave>(`${taskPath}/problems/${encodeURIComponent(qId)}/stem-save`, {
        stem, expected_workflow_revision: expectedRevision, retry_of: retryOf,
      }),
    onSuccess: async response => {
      await client.cancelQueries({ queryKey });
      client.setQueryData<QuestionStemSave[]>(queryKey, rows => [
        ...(rows ?? []).filter(save => save.q_id !== response.q_id), response,
      ]);
      void client.invalidateQueries({ queryKey });
    },
    onError: () => {
      // A missing response can still mean the server accepted the intent.
      // Resolve from the durable status without launching another request.
      void client.invalidateQueries({ queryKey });
    },
  });
  return {
    byQuestion: new Map(saves.data?.map(save => [save.q_id, save])),
    busy: submit.isPending || Boolean(refreshRequired || saves.data?.some(inProgress)),
    submittingQuestion: submit.isPending ? submit.variables.qId : undefined,
    submit: submit.mutateAsync,
  };
}
