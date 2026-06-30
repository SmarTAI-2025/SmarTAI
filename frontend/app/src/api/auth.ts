import { clearAuthToken, getJSON, postJSON, setAuthToken } from "./client";
import type { AuthResponse, LoginRequest, RefreshResponse, RegisterRequest, StatusResponse, User } from "@/types";

export async function login(request: LoginRequest): Promise<AuthResponse> {
  const response = await postJSON<AuthResponse, LoginRequest>("/auth/login", request);
  setAuthToken(response.token);
  return response;
}

export async function register(request: RegisterRequest): Promise<AuthResponse> {
  const response = await postJSON<AuthResponse, RegisterRequest>("/auth/register", request);
  setAuthToken(response.token);
  return response;
}

export async function getCurrentUser(): Promise<User> {
  return getJSON<User>("/auth/me");
}

export async function refreshToken(): Promise<RefreshResponse> {
  const response = await postJSON<RefreshResponse>("/auth/refresh");
  setAuthToken(response.token);
  return response;
}

export async function logout(): Promise<StatusResponse> {
  try {
    return await postJSON<StatusResponse>("/auth/logout");
  } finally {
    clearAuthToken();
  }
}
