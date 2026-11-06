import { useQuery } from "@tanstack/react-query";
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

export function useAdminOverview() {
  return useQuery<AdminOverview>({ queryKey: adminKeys.overview(), queryFn: adminApi.adminOverview });
}

export function useAdminAudit() {
  return useQuery<AdminAuditEntry[]>({ queryKey: adminKeys.audit(), queryFn: adminApi.adminListAudit });
}


export type { AdminUser, AdminOverview, AdminAuditEntry, AdminMetricsResult, AdminUsersPage };
