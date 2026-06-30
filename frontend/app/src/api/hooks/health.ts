import { useQuery } from "@tanstack/react-query";
import * as healthApi from "@/api/health";
import { healthKeys } from "./keys";

export function useRootStatus() {
  return useQuery({
    queryKey: healthKeys.root,
    queryFn: healthApi.getRootStatus,
  });
}

export function useHealthStatus(options: { refetchInterval?: number | false } = {}) {
  return useQuery({
    queryKey: healthKeys.status,
    queryFn: healthApi.getHealthStatus,
    refetchInterval: options.refetchInterval,
  });
}
