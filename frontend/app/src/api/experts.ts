import { deleteJSON, getJSON, postJSON, putJSON, type AuthAwareRequestConfig } from "./client";
import type {
  AddExpertKeyRequest,
  ByokConnection,
  ReasoningEffort,
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

export function listExperts(frontier = false): Promise<ExpertConfig[]> {
  return getJSON<ExpertConfig[]>("/experts/available", { _authScope: frontier ? "frontier" : "teacher" } as AuthAwareRequestConfig);
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
    { timeout: 75_000 },
  );
}

export function removeExpert(providerId: string): Promise<ExpertMutationResponse> {
  return deleteJSON<ExpertMutationResponse>(`/experts/${encodeURIComponent(providerId)}`);
}

export async function verifyExpertImage(providerId: string): Promise<Pick<ExpertConfig, "image_capability_status" | "image_checked_at" | "image_reason">> {
  // Allow the default 60s server probe deadline to return its persisted result.
  return postJSON(`/experts/${encodeURIComponent(providerId)}/verify-image`, {}, { timeout: 75_000 });
}


const connectionUrl = "/experts/connections";

export type SaveByokConnection = Omit<AddExpertKeyRequest, "model">;
type EditByokConnection = Pick<SaveByokConnection, "display_name" | "rpm"> & { api_key?: string };
export type ByokChoice = { connectionId: string; model: string; effort: ReasoningEffort | null };

export async function listByokConnections(): Promise<ByokConnection[]> {
  const rows = await getJSON<ByokConnection[]>(connectionUrl);
  return Array.isArray(rows)
    ? rows.filter((row) => row && typeof row.id === "string" && Array.isArray(row.models))
    : [];
}

export function saveByokConnection(request: SaveByokConnection): Promise<ByokConnection> {
  return postJSON(connectionUrl, request, { timeout: 25_000 });
}

export function updateByokConnection(connectionId: string, request: EditByokConnection): Promise<ByokConnection> {
  return putJSON(`${connectionUrl}/${encodeURIComponent(connectionId)}`, request, { timeout: 25_000 });
}

export function refreshByokConnection(connectionId: string): Promise<ByokConnection> {
  return postJSON(`${connectionUrl}/${encodeURIComponent(connectionId)}/refresh`, {}, { timeout: 25_000 });
}

export function removeByokConnection(connectionId: string): Promise<{ status: string }> {
  return deleteJSON(`${connectionUrl}/${encodeURIComponent(connectionId)}`);
}

export function bindByokModel(choice: ByokChoice): Promise<{ provider_id: string }> {
  return postJSON(`${connectionUrl}/${encodeURIComponent(choice.connectionId)}/model`, {
    model: choice.model,
    reasoning_effort: choice.effort,
  });
}
