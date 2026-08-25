import { deleteJSON, getJSON, postJSON, putJSON } from "./client";
import type {
  BaiduOCRConfiguration,
  BaiduOCRCredentialDeleteResponse,
  BaiduOCRCredentialMutationResponse,
  BaiduOCRVerificationResponse,
  SaveBaiduOCRCredentialsRequest,
} from "@/types";

const ROOT = "/ocr/providers/baidu-unlimited-ocr";

export function getBaiduOCRConfiguration(): Promise<BaiduOCRConfiguration> {
  return getJSON<BaiduOCRConfiguration>(`${ROOT}/configuration`);
}

export function saveBaiduOCRCredentials(
  request: SaveBaiduOCRCredentialsRequest,
): Promise<BaiduOCRCredentialMutationResponse> {
  return putJSON<BaiduOCRCredentialMutationResponse, SaveBaiduOCRCredentialsRequest>(
    ROOT,
    request,
  );
}

export function verifyBaiduOCRCredentials(
  credentialId: string,
): Promise<BaiduOCRVerificationResponse> {
  return postJSON<BaiduOCRVerificationResponse>(
    `${ROOT}/${encodeURIComponent(credentialId)}/verify`,
  );
}

export function deleteBaiduOCRCredentials(
  credentialId: string,
): Promise<BaiduOCRCredentialDeleteResponse> {
  return deleteJSON<BaiduOCRCredentialDeleteResponse>(
    `${ROOT}/${encodeURIComponent(credentialId)}`,
  );
}
