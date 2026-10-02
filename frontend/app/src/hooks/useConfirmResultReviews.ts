import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { updateCorrectionReview } from "@/api/tasks";
import { taskKeys } from "@/api/hooks/keys";

export type ResultReviewConfirmation = {
  studentId: string;
  qId: string;
  score: number;
  comment: string;
};

/** Revision changes are chained; a failed write stops the batch without replaying it. */
export function useConfirmResultReviews() {
  const client = useQueryClient();
  const [progress, setProgress] = useState({ completed: 0, total: 0 });
  const mutation = useMutation({
    mutationFn: async ({ taskId, revision, entries }: {
      taskId: string;
      revision: number;
      entries: ResultReviewConfirmation[];
    }) => {
      setProgress({ completed: 0, total: entries.length });
      let currentRevision = revision;
      for (const [index, entry] of entries.entries()) {
        const response = await updateCorrectionReview(taskId, entry.studentId, entry.qId, {
          expected_workflow_revision: currentRevision,
          teacher_score: entry.score,
          teacher_comment: entry.comment,
          confirm: true,
        });
        currentRevision = response.workflow_revision;
        setProgress({ completed: index + 1, total: entries.length });
      }
      return currentRevision;
    },
    onSettled: async (_data, _error, { taskId }) => {
      await Promise.all([
        taskKeys.detail(taskId), taskKeys.result(taskId), taskKeys.finalization(taskId),
        taskKeys.comments(taskId), taskKeys.state(taskId), taskKeys.list(),
      ].map((queryKey) => client.invalidateQueries({ queryKey })));
    },
  });
  return { ...mutation, progress };
}
