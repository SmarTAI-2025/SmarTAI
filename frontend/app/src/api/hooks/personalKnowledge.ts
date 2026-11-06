import { useQuery } from "@tanstack/react-query";
import * as personalKnowledgeApi from "@/api/personalKnowledge";
import { personalKnowledgeKeys } from "./keys";

export function useKnowledgeStorageUsage() {
  return useQuery({
    queryKey: personalKnowledgeKeys.usage(),
    queryFn: personalKnowledgeApi.getKnowledgeStorageUsage,
    refetchInterval: (query) => {
      const usage = query.state.data;
      return usage && (usage.cleanup_pending_count > 0 || usage.retrying_cleanup_count > 0)
        ? 3_000
        : false;
    },
  });
}
