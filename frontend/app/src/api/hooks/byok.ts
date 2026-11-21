import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as expertsApi from "@/api/experts";
import { expertKeys } from "./keys";
import { useFrontierWorkspace } from "@/lib/frontierContext";


export function useByokConnections(enabled = true) {
  const frontier = useFrontierWorkspace();
  return useQuery({
    queryKey: frontier ? [...expertKeys.connections(), "frontier"] : expertKeys.connections(),
    queryFn: expertsApi.listByokConnections,
    enabled: enabled && !frontier,
    staleTime: 60_000,
    retry: false,
  });
}

export function useBindByokModel() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: expertsApi.bindByokModel,
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: expertKeys.list() }),
        client.invalidateQueries({ queryKey: expertKeys.stage() }),
      ]);
    },
  });
}


export function useSaveByokConnection() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, request }: { id?: string; request: expertsApi.SaveByokConnection }) =>
      id ? expertsApi.updateByokConnection(id, request) : expertsApi.saveByokConnection(request),
    onSuccess: () => client.invalidateQueries({ queryKey: expertKeys.all }),
  });
}
