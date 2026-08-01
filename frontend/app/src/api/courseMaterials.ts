import { deleteJSON, getJSON, postJSON, postMultipart, putJSON } from "./client";
import type {
  CourseMaterial,
  CourseMaterialGroup,
  CourseMaterialGroupListResponse,
  CourseMaterialListParams,
  CourseMaterialListResponse,
  CreateCourseMaterialGroupInput,
  DeleteCourseMaterialGroupResponse,
  DeleteCourseMaterialResponse,
  UpdateCourseMaterialGroupInput,
  UpdateCourseMaterialInput,
  UploadCourseMaterialInput,
} from "@/types";

export function listCourseMaterials(
  params: CourseMaterialListParams,
): Promise<CourseMaterialListResponse> {
  return getJSON<CourseMaterialListResponse>("/course-materials/", { params });
}

export function uploadCourseMaterial(input: UploadCourseMaterialInput): Promise<CourseMaterial & { created: boolean }> {
  return postMultipart<CourseMaterial & { created: boolean }>("/course-materials/", input.file, {
    fields: {
      course_id: input.courseId,
      group_id: input.groupId,
      category: input.category,
      labels: JSON.stringify(input.labels),
    },
  });
}

export function updateCourseMaterial(input: UpdateCourseMaterialInput): Promise<CourseMaterial> {
  return putJSON<CourseMaterial, UpdateCourseMaterialInput["body"]>(
    `/course-materials/${input.materialId}`,
    input.body,
  );
}

export function deleteCourseMaterial(
  materialId: string,
  confirmReferenced: boolean,
): Promise<DeleteCourseMaterialResponse> {
  return deleteJSON<DeleteCourseMaterialResponse>(`/course-materials/${materialId}`, {
    params: { confirm_referenced: confirmReferenced },
  });
}

export function listCourseMaterialGroups(q = ""): Promise<CourseMaterialGroupListResponse> {
  return getJSON<CourseMaterialGroupListResponse>("/course-materials/groups", {
    params: q ? { q } : undefined,
  });
}

export function createCourseMaterialGroup(
  input: CreateCourseMaterialGroupInput,
): Promise<CourseMaterialGroup & { created: boolean }> {
  return postJSON<CourseMaterialGroup & { created: boolean }, CreateCourseMaterialGroupInput>(
    "/course-materials/groups",
    input,
  );
}

export function updateCourseMaterialGroup(
  input: UpdateCourseMaterialGroupInput,
): Promise<CourseMaterialGroup> {
  return putJSON<CourseMaterialGroup, UpdateCourseMaterialGroupInput["body"]>(
    `/course-materials/groups/${input.groupId}`,
    input.body,
  );
}

export function deleteCourseMaterialGroup(
  groupId: string,
): Promise<DeleteCourseMaterialGroupResponse> {
  return deleteJSON<DeleteCourseMaterialGroupResponse>(`/course-materials/groups/${groupId}`);
}
