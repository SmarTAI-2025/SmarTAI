import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as adminApi from "@/api/admin";
import { adminKeys } from "./keys";
import type { AdminUser, Invite } from "@/types/education";

export function useAdminUsers(filter?: { role?: string; is_active?: boolean }) {
  return useQuery({
    queryKey: adminKeys.users(filter),
    queryFn: () => adminApi.adminListUsers(filter),
  });
}

export function useAdminInvites() {
  return useQuery({
    queryKey: adminKeys.invites(),
    queryFn: adminApi.adminListInvites,
  });
}

export function useAdminSetActive() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, isActive }: { userId: string; isActive: boolean }) =>
      adminApi.adminSetActive(userId, isActive),
    onSuccess: () => {
      // Any filter view of users may have changed; invalidate the whole set.
      queryClient.invalidateQueries({ queryKey: adminKeys.all });
    },
  });
}

export function useAdminCreateInvite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: adminApi.adminCreateInvite,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: adminKeys.invites() });
    },
  });
}

export type { AdminUser, Invite };
