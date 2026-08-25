import { clearAuthToken, getAuthToken, getJSON, postJSON, setAuthToken } from "./client";
import type { AuthResponse, EmailRegistrationRequest, EmailRegistrationResponse, EmailRegistrationVerifyResponse, LoginRequest, RefreshResponse, RegisterRequest, StatusResponse, User } from "@/types";

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

export async function requestRegistration(request: EmailRegistrationRequest): Promise<EmailRegistrationResponse> {
  return postJSON<EmailRegistrationResponse, EmailRegistrationRequest>("/auth/register/request", request);
}

export async function resendRegistration(requestId: string): Promise<EmailRegistrationResponse> {
  return postJSON<EmailRegistrationResponse, { request_id: string }>("/auth/register/resend", { request_id: requestId });
}

export async function verifyRegistration(token: string): Promise<EmailRegistrationVerifyResponse> {
  return postJSON<EmailRegistrationVerifyResponse, { token: string }>("/auth/register/verify", { token });
}

export async function getCurrentUser(): Promise<User> {
  return getJSON<User>("/auth/me");
}

export async function refreshToken(): Promise<RefreshResponse> {
  const response = await postJSON<RefreshResponse>("/auth/refresh");
  setAuthToken(response.token);
  return response;
}

export async function restoreSession(): Promise<User> {
  if (!getAuthToken()) {
    const response = await refreshToken();
    if (response.user) {
      return response.user;
    }
  }
  return getCurrentUser();
}

export async function logout(): Promise<StatusResponse> {
  try {
    return await postJSON<StatusResponse>("/auth/logout");
  } finally {
    clearAuthToken();
  }
}
