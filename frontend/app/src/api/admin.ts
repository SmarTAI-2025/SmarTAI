/**
 * Admin API client: user management + invitations. Mirrors backend/api/admin.py.
 */
import { apiClient } from "./client";
import type { AdminUser, Invite } from "@/types/education";

export async function adminListUsers(params?: { role?: string; is_active?: boolean }): Promise<AdminUser[]> {
  const { data } = await apiClient.get<AdminUser[]>("/admin/users", { params });
  return data;
}

export async function adminSetActive(userId: string, isActive: boolean): Promise<AdminUser> {
  const { data } = await apiClient.patch<AdminUser>(`/admin/users/${userId}/active`, { is_active: isActive });
  return data;
}

export async function adminCreateInvite(input: {
  email?: string;
  role: "teacher" | "student" | "admin";
  course_id?: string | null;
  expires_in_hours?: number;
}): Promise<{ invite_code: string; role: string; expires_at: number }> {
  const { data } = await apiClient.post("/admin/invites", input);
  return data;
}

export async function adminListInvites(): Promise<Invite[]> {
  const { data } = await apiClient.get<Invite[]>("/admin/invites");
  return data;
}
