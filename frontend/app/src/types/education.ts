/** Contracts used only by the private administrator console. */
import type { UserRole } from "./auth";

export interface AdminUser {
  closure?: { id: string; status: string; mode: string; error_code?: string | null } | null;
  id: string;
  username: string;
  email: string;
  role: UserRole;
  is_active: boolean;
  is_read_only?: boolean;
  created_at: number;
  updated_at?: number;
}
