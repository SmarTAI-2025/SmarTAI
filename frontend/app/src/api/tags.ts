import { deleteJSON, getJSON, postJSON, putJSON } from "./client";
import type { CatalogSearchResponse, CreateTagResponse, TagColor, TaskTag } from "@/types";

export function listTags(): Promise<TaskTag[]> {
  return getJSON<TaskTag[]>("/tags/");
}

export function searchTags(query: string, pageSize = 20): Promise<CatalogSearchResponse<TaskTag>> {
  return getJSON<CatalogSearchResponse<TaskTag>>("/tags/search", {
    params: { q: query, page: 1, page_size: pageSize },
  });
}

export function createTag(body: {
  name: string;
  color?: TagColor;
  force_create?: boolean;
}): Promise<CreateTagResponse> {
  return postJSON<CreateTagResponse, {
    name: string;
    color?: TagColor;
    force_create?: boolean;
  }>("/tags/", body);
}

export function updateTag(tagId: string, patch: { name?: string; color?: TagColor }): Promise<TaskTag> {
  return putJSON<TaskTag, { name?: string; color?: TagColor }>(`/tags/${tagId}`, patch);
}

export function deleteTag(tagId: string): Promise<{ status: "success"; tag_id: string; affected_tasks: number }> {
  return deleteJSON(`/tags/${tagId}`);
}
