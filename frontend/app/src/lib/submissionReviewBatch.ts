/** Use each successful response's revision; stop instead of retrying stale data. */
export async function confirmSubmissionBatch<T>(
  items: T[],
  workflowRevision: number,
  confirm: (item: T, revision: number) => Promise<{ workflow_revision: number }>,
  onProgress: (completed: number) => void = () => undefined,
) {
  let completed = 0;
  for (const item of items) {
    try {
      const result = await confirm(item, workflowRevision);
      workflowRevision = result.workflow_revision;
      completed += 1;
      onProgress(completed);
    } catch (error) {
      return { completed, remaining: items.length - completed, failedItem: item, error };
    }
  }
  return { completed, remaining: 0, failedItem: undefined, error: undefined };
}
