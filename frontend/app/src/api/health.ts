import { getJSON } from "./client";
import type { HealthStatus, RootStatus } from "@/types";

export function getRootStatus(): Promise<RootStatus> {
  return getJSON<RootStatus>("/");
}

export function getHealthStatus(): Promise<HealthStatus> {
  return getJSON<HealthStatus>("/health");
}
