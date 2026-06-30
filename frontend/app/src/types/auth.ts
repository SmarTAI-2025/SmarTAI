export type UserRole = "teacher" | "student" | "admin";

export interface User {
  id: string;
  username: string;
  email: string;
  role: UserRole;
  course_ids: string[];
  created_at: number;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface RegisterRequest extends LoginRequest {
  email: string;
  role?: UserRole;
  invite_code?: string | null;
}

export interface AuthResponse {
  token: string;
  user: User;
  user_id?: string;
}

export interface RefreshResponse {
  token: string;
}

export interface StatusResponse {
  status: string;
}
