import {
  APIError,
  clearAuthToken,
  getAuthToken,
  getJSON,
  postJSON,
  setAuthToken,
  type AuthAwareRequestConfig,
} from "./client";
import type { AuthResponse, EmailRegistrationRequest, EmailRegistrationResponse, EmailRegistrationVerifyResponse, LoginRequest, PasswordResetConfirmResponse, PasswordResetRequest, PasswordResetRequestResponse, RefreshResponse, StatusResponse, User } from "@/types";

const PUBLIC_AUTH_REQUEST_CONFIG: AuthAwareRequestConfig = Object.freeze({
  _skipAuthHeader: true,
  _skipAuthRefresh: true,
});

export async function login(request: LoginRequest): Promise<AuthResponse> {
  const response = await postJSON<AuthResponse, LoginRequest>("/auth/login", request);
  setAuthToken(response.token);
  return response;
}

export async function requestRegistration(request: EmailRegistrationRequest): Promise<EmailRegistrationResponse> {
  const response = await postJSON<unknown, EmailRegistrationRequest>(
    "/auth/register/request",
    request,
    PUBLIC_AUTH_REQUEST_CONFIG,
  );
  return parseRegistrationResponse(response);
}

export async function resendRegistration(requestId: string): Promise<EmailRegistrationResponse> {
  const response = await postJSON<unknown, { request_id: string }>(
    "/auth/register/resend",
    { request_id: requestId },
    PUBLIC_AUTH_REQUEST_CONFIG,
  );
  return parseRegistrationResponse(response);
}

export async function verifyRegistration(token: string): Promise<EmailRegistrationVerifyResponse> {
  const response = await postJSON<unknown, { token: string }>(
    "/auth/register/verify",
    { token },
    PUBLIC_AUTH_REQUEST_CONFIG,
  );
  return parseRegistrationVerifyResponse(response);
}

export async function requestPasswordReset(request: PasswordResetRequest): Promise<PasswordResetRequestResponse> {
  const response = await postJSON<unknown, PasswordResetRequest>(
    "/auth/password-reset/request",
    request,
    PUBLIC_AUTH_REQUEST_CONFIG,
  );
  return parsePasswordResetRequestResponse(response);
}

export async function confirmPasswordReset(token: string, newPassword: string): Promise<PasswordResetConfirmResponse> {
  const response = await postJSON<unknown, { token: string; new_password: string }>(
    "/auth/password-reset/confirm",
    { token, new_password: newPassword },
    PUBLIC_AUTH_REQUEST_CONFIG,
  );
  return parsePasswordResetConfirmResponse(response);
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

function parseRegistrationResponse(value: unknown): EmailRegistrationResponse {
  const response = exactRecord(value, [
    "status",
    "request_id",
    "expires_in_seconds",
    "resend_after_seconds",
  ]);
  if (
    !response
    || response.status !== "verification_required"
    || typeof response.request_id !== "string"
    || !response.request_id.trim()
    || response.request_id.length > 512
    || !isNonNegativeFiniteNumber(response.expires_in_seconds)
    || !isNonNegativeFiniteNumber(response.resend_after_seconds)
  ) {
    throw invalidPublicAuthResponse();
  }
  return {
    status: "verification_required",
    request_id: response.request_id,
    expires_in_seconds: response.expires_in_seconds,
    resend_after_seconds: response.resend_after_seconds,
  };
}

function parseRegistrationVerifyResponse(value: unknown): EmailRegistrationVerifyResponse {
  const response = exactRecord(value, ["status"]);
  if (!response || (response.status !== "registered" && response.status !== "already_verified")) {
    throw invalidPublicAuthResponse();
  }
  return { status: response.status };
}

function parsePasswordResetRequestResponse(value: unknown): PasswordResetRequestResponse {
  const response = exactRecord(value, ["status", "expires_in_seconds", "resend_after_seconds"]);
  if (
    !response
    || response.status !== "reset_link_requested"
    || !isNonNegativeFiniteNumber(response.expires_in_seconds)
    || !isNonNegativeFiniteNumber(response.resend_after_seconds)
  ) {
    throw invalidPublicAuthResponse();
  }
  return {
    status: "reset_link_requested",
    expires_in_seconds: response.expires_in_seconds,
    resend_after_seconds: response.resend_after_seconds,
  };
}

function parsePasswordResetConfirmResponse(value: unknown): PasswordResetConfirmResponse {
  const response = exactRecord(value, ["status"]);
  if (!response || response.status !== "password_reset") {
    throw invalidPublicAuthResponse();
  }
  return { status: "password_reset" };
}

function exactRecord(value: unknown, keys: readonly string[]): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const response = value as Record<string, unknown>;
  const actualKeys = Object.keys(response);
  if (actualKeys.length !== keys.length || actualKeys.some((key) => !keys.includes(key))) return null;
  return response;
}

function isNonNegativeFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function invalidPublicAuthResponse(): APIError {
  return new APIError(502, "public_auth_response_invalid", {
    detail: { code: "public_auth_response_invalid" },
  });
}
