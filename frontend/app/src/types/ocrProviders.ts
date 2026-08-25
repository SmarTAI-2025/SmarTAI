export type OCRCredentialVerificationStatus =
  | "not_configured"
  | "unverified"
  | "credentials_verified"
  | "failed";

export interface BaiduOCRConfiguration {
  credential_id: string | null;
  provider_type: "baidu_unlimited_ocr";
  credentials_configured: boolean;
  verification_status: OCRCredentialVerificationStatus;
  last_checked_at: string | null;
  verification_error_code: string | null;
}

export interface SaveBaiduOCRCredentialsRequest {
  api_key: string;
  secret_key: string;
}

export interface BaiduOCRCredentialMutationResponse extends BaiduOCRConfiguration {
  status: string;
}

export interface BaiduOCRCredentialDeleteResponse {
  status: "credentials_deleted";
  credential_id: string;
  provider_type: "baidu_unlimited_ocr";
}

export interface BaiduOCRVerificationResponse {
  status: "credentials_verified";
  credential_id: string;
  provider_type: "baidu_unlimited_ocr";
  verification_scope: "credentials_only";
  service_readiness: "not_tested";
  verified_at: string;
}
