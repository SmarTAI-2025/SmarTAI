export type UserRole = "teacher" | "student" | "admin";

export interface User {
  id: string;
  username: string;
  email: string;
  role: UserRole;
  is_active: boolean;
  created_at: number;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface RegisterRequest extends LoginRequest {
  email: string;
  role?: "teacher" | "student";
  invite_code?: string | null;
}

export interface AuthResponse {
  token: string;
  user: User;
  user_id?: string;
}

export interface RefreshResponse {
  token: string;
  user?: User;
}

export interface InviteRequest {
  email?: string;
  role: "teacher" | "student";
  course_id?: string | null;
  expires_in_hours?: number;
}

export interface InviteResponse {
  invite_code: string;
  expires_at: number;
}

export interface StatusResponse {
  status: string;
}
