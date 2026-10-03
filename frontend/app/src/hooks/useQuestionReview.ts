import { useEffect, useRef, useState } from "react";
import { useUpdateProblem } from "@/api/hooks/tasks";
import type { ProblemInfo } from "@/types";

/** Share the revision between edits and sequential review confirmations. */
export function useQuestionReview(taskId: string, workflowRevision?: number) {
  const updateProblem = useUpdateProblem();
  const latestRevision = useRef(workflowRevision);
  const taskRef = useRef(taskId);
  const inFlight = useRef(false);
  const [confirming, setConfirming] = useState(false);
  const [failure, setFailure] = useState<{ problem: ProblemInfo; completed: number } | null>(null);

  useEffect(() => {
    if (taskRef.current !== taskId) {
      taskRef.current = taskId;
      latestRevision.current = workflowRevision;
      setFailure(null);
    } else if (workflowRevision !== undefined && (latestRevision.current === undefined || workflowRevision > latestRevision.current)) {
      latestRevision.current = workflowRevision;
    }
  }, [taskId, workflowRevision]);

  async function confirm(problems: ProblemInfo[]) {
    if (inFlight.current || updateProblem.isPending) return false;
    inFlight.current = true;
    setConfirming(true);
    setFailure(null);
    let completed = 0;
    let expectedWorkflowRevision = latestRevision.current;
    try {
      for (const problem of problems) {
        try {
          const response = await updateProblem.mutateAsync({
            taskId, qId: problem.q_id,
            expectedWorkflowRevision,
            review_status: "confirmed",
          });
          latestRevision.current = response.workflow_revision;
          expectedWorkflowRevision = response.workflow_revision;
          completed += 1;
        } catch {
          setFailure({ problem, completed });
          return false;
        }
      }
      return true;
    } finally {
      inFlight.current = false;
      setConfirming(false);
    }
  }

  return { updateProblem, latestRevision, confirming, confirm, failure };
}
