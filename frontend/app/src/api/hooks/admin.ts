import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as adminApi from "@/api/admin";
import { adminKeys } from "./keys";
import type { AdminUser } from "@/types/education";
import type { AdminAuditEntry, AdminOverview, AdminMetricsResult, AdminUsersPage } from "@/api/admin";

export function useAdminUsers(filter?: { role?: string; is_active?: boolean; search?: string; page?: number; page_size?: number }) {
  return useQuery({
    queryKey: adminKeys.users(filter),
    queryFn: () => adminApi.adminListUsers(filter),
  });
}


export function useAdminSetActive() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, isActive, reason, note }: { userId: string; isActive: boolean; reason?: string; note?: string }) =>
      adminApi.adminSetActive(userId, isActive, reason, note),
    onSuccess: () => {
      // Any filter view of users may have changed; invalidate the whole set.
      queryClient.invalidateQueries({ queryKey: adminKeys.all });
    },
  });
}

export function useAdminRevokeSessions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, reason }: { userId: string; reason?: string }) => adminApi.adminRevokeSessions(userId, reason),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: adminKeys.audit() }),
  });
}

export function useAdminRequestPasswordReset() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, reason }: { userId: string; reason?: string }) => adminApi.adminRequestPasswordReset(userId, reason),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: adminKeys.audit() }),
  });
}

export function useAdminOverview() {
  return useQuery<AdminOverview>({ queryKey: adminKeys.overview(), queryFn: adminApi.adminOverview });
}

export function useAdminMetrics(input: { start?: number; end?: number; granularity?: "day" | "week"; metrics?: string[] } = {}) {
  return useQuery<AdminMetricsResult>({
    queryKey: ["admin", "metrics", input],
    queryFn: () => adminApi.adminMetricsQuery(input),
  });
}

export function useAdminAudit() {
  return useQuery<AdminAuditEntry[]>({ queryKey: adminKeys.audit(), queryFn: adminApi.adminListAudit });
}


export type { AdminUser, AdminOverview, AdminAuditEntry, AdminMetricsResult, AdminUsersPage };
