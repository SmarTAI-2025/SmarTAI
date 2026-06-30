import { deleteJSON, getJSON, postJSON } from "./client";
import type { AddExpertKeyRequest, ExpertConfig, ExpertMutationResponse } from "@/types";

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

export function selectExpert(providerId: string, enabled: boolean): Promise<ExpertMutationResponse> {
  return postJSON<ExpertMutationResponse>("/experts/select", {
    provider_id: providerId,
    enabled,
  });
}

export function removeExpert(providerId: string): Promise<ExpertMutationResponse> {
  return deleteJSON<ExpertMutationResponse>(`/experts/${providerId}`);
}
