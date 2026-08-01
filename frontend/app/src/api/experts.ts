import { deleteJSON, getJSON, postJSON, putJSON } from "./client";
import type {
  AddExpertKeyRequest,
  ExpertConfig,
  ExpertMutationResponse,
  ExpertVerificationResponse,
  ProviderCatalogItem,
  UpdateExpertRequest,
} from "@/types";

export function addExpertKey(request: AddExpertKeyRequest): Promise<ExpertMutationResponse> {
  return postJSON<ExpertMutationResponse, AddExpertKeyRequest>("/experts/keys", {
    ...request,
    max_concurrent: request.max_concurrent ?? 5,
    rpm: request.rpm ?? 0,
  });
}

export function listExperts(): Promise<ExpertConfig[]> {
  return getJSON<ExpertConfig[]>("/experts/available");
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

export function updateExpert(
  providerId: string,
  request: UpdateExpertRequest,
): Promise<ExpertMutationResponse> {
  return putJSON<ExpertMutationResponse, UpdateExpertRequest>(
    `/experts/${encodeURIComponent(providerId)}`,
    {
      ...request,
      max_concurrent: request.max_concurrent ?? 5,
      rpm: request.rpm ?? 0,
    },
  );
}

export function verifyExpert(providerId: string): Promise<ExpertVerificationResponse> {
  return postJSON<ExpertVerificationResponse>(
    `/experts/${encodeURIComponent(providerId)}/verify`,
  );
}

export function removeExpert(providerId: string): Promise<ExpertMutationResponse> {
  return deleteJSON<ExpertMutationResponse>(`/experts/${encodeURIComponent(providerId)}`);
}
