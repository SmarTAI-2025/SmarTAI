import { deleteJSON, getJSON, postJSON, putJSON } from "./client";
import type {
  AddExpertKeyRequest,
  ExpertConfig,
  ExpertMutationResponse,
  ExpertVerificationResponse,
  ProviderCatalogItem,
  SetDefaultExpertResponse,
  UpdateExpertRequest,
} from "@/types";

export function addExpertKey(request: AddExpertKeyRequest): Promise<ExpertMutationResponse> {
  return postJSON<ExpertMutationResponse, AddExpertKeyRequest>("/experts/keys", {
    ...request,
    rpm: request.rpm ?? 0,
  });
}

export function listExperts(): Promise<ExpertConfig[]> {
  return getJSON<ExpertConfig[]>("/experts/available");
}

export function listStageProviders(): Promise<ExpertConfig[]> {
  return getJSON<ExpertConfig[]>("/experts/stage-options");
}

export function listProviderCatalog(): Promise<ProviderCatalogItem[]> {
  return getJSON<ProviderCatalogItem[]>("/experts/catalog");
}

export function selectExpert(providerId: string, enabled: boolean): Promise<ExpertMutationResponse> {
  return postJSON<ExpertMutationResponse>("/experts/select", {
    provider_id: providerId,
    enabled,
  });
}

export function setDefaultExpert(providerId: string): Promise<SetDefaultExpertResponse> {
  return putJSON<SetDefaultExpertResponse, { provider_id: string }>("/experts/default", {
    provider_id: providerId,
  });
}

export function updateExpert(
  providerId: string,
  request: UpdateExpertRequest,
): Promise<ExpertMutationResponse> {
  return putJSON<ExpertMutationResponse, UpdateExpertRequest>(
    `/experts/${encodeURIComponent(providerId)}`,
    {
      ...request,
      rpm: request.rpm ?? 0,
    },
  );
}

export function verifyExpert(providerId: string): Promise<ExpertVerificationResponse> {
  return postJSON<ExpertVerificationResponse>(
    `/experts/${encodeURIComponent(providerId)}/verify`,
    {},
    { timeout: 45_000 },
  );
}

export function removeExpert(providerId: string): Promise<ExpertMutationResponse> {
  return deleteJSON<ExpertMutationResponse>(`/experts/${encodeURIComponent(providerId)}`);
}

export async function verifyExpertImage(providerId: string): Promise<Pick<ExpertConfig, "image_capability_status" | "image_checked_at" | "image_reason">> {
  // Allow the default 30s server probe deadline to return its persisted result.
  return postJSON(`/experts/${encodeURIComponent(providerId)}/verify-image`, {}, { timeout: 45_000 });
}
