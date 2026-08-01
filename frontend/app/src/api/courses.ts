import { getJSON, postJSON } from "./client";
import type { CatalogSearchResponse, Course, CreateCourseResponse } from "@/types";

export function listCourses(): Promise<Course[]> {
  return getJSON<Course[]>("/courses/");
}

export function searchCourses(query: string, pageSize = 20): Promise<CatalogSearchResponse<Course>> {
  return getJSON<CatalogSearchResponse<Course>>("/courses/search", {
    params: { q: query, page: 1, page_size: pageSize },
  });
}

export function createCourse(body: {
  name: string;
  force_create?: boolean;
}): Promise<CreateCourseResponse> {
  return postJSON<CreateCourseResponse, {
    name: string;
    force_create?: boolean;
  }>("/courses/", body);
}
