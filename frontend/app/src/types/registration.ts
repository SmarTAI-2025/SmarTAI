export type RegistrationTransport = "api" | "development_mock";

export interface RegistrationRequest {
  username: string;
  email: string;
  password: string;
}

export interface RegistrationRequestResponse {
  status: "verification_required";
  request_id: string;
  expires_in_seconds: number;
  resend_after_seconds: number;
}

export interface RegistrationRequestResult extends RegistrationRequestResponse {
  transport: RegistrationTransport;
}

export interface RegistrationResendRequest {
  request_id: string;
}

export type RegistrationResendResponse = RegistrationRequestResponse;

export interface RegistrationVerificationRequest {
  token: string;
}

export interface RegistrationVerificationResponse {
  status: "registered" | "already_verified";
}

export interface PendingRegistrationFlow {
  version: 1;
  requestId: string;
  username: string;
  email: string;
  createdAt: number;
  expiresAt: number;
  resendAvailableAt: number;
  transport: RegistrationTransport;
}
