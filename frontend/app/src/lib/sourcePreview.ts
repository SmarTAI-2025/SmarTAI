import type { SourceFileDescriptor, SourcePreviewKind } from "@/types/sourcePreview";

const IMAGE_EXTENSIONS = new Set(["bmp", "gif", "jpeg", "jpg", "png", "webp"]);

const IMAGE_MIME_TYPES = new Set([
  "image/bmp",
  "image/gif",
  "image/jpeg",
  "image/png",
  "image/webp",
]);

export type SourcePreviewMockVariant = "pdf" | "image" | "processing" | "unavailable" | "error" | null;

export interface SourcePreviewMockScenario {
  descriptor: SourceFileDescriptor;
  variant: SourcePreviewMockVariant;
  mockEnabled: boolean;
}

export function getSourcePreviewMockVariant(value: string | null): SourcePreviewMockVariant {
  return value === "pdf"
    || value === "image"
    || value === "processing"
    || value === "unavailable"
    || value === "error"
    ? value
    : null;
}

export function buildSourcePreviewMockScenario({
  scope,
  sourceId,
  displayName,
  taskFinalized,
  variant,
}: {
  scope: "problem" | "submission";
  sourceId: string;
  displayName?: string | null;
  taskFinalized: boolean;
  variant: SourcePreviewMockVariant;
}): SourcePreviewMockScenario {
  const normalizedName = displayName?.trim() ?? "";
  const mockEnabled = import.meta.env.DEV || import.meta.env.MODE === "test";
  const forcedKind = variant === "pdf" ? "pdf" : variant === "image" ? "image" : null;
  const previewKind = forcedKind ?? inferSourcePreviewKind(normalizedName);
  const descriptor = baseDescriptor(scope, sourceId, normalizedName, previewKind);

  if (taskFinalized) {
    return {
      descriptor: { ...descriptor, status: "unavailable", unavailable_reason: "task_finalized" },
      variant,
      mockEnabled,
    };
  }
  if (!normalizedName) {
    return {
      descriptor: { ...descriptor, status: "unavailable", unavailable_reason: "missing" },
      variant,
      mockEnabled,
    };
  }
  if (!mockEnabled || variant === "unavailable") {
    return {
      descriptor: { ...descriptor, status: "unavailable", unavailable_reason: "not_persisted" },
      variant,
      mockEnabled,
    };
  }
  if (previewKind === "unsupported") {
    return {
      descriptor: { ...descriptor, status: "unavailable", unavailable_reason: "unsupported_type" },
      variant,
      mockEnabled,
    };
  }
  if (variant === "processing") {
    return {
      descriptor: { ...descriptor, status: "processing", unavailable_reason: null },
      variant,
      mockEnabled,
    };
  }
  return {
    descriptor: { ...descriptor, status: "available", unavailable_reason: null },
    variant,
    mockEnabled,
  };
}

export function inferSourcePreviewKind(displayName: string, mimeType?: string | null): SourcePreviewKind {
  const normalizedMime = mimeType?.trim().toLowerCase() ?? "";
  if (normalizedMime === "application/pdf") return "pdf";
  if (IMAGE_MIME_TYPES.has(normalizedMime)) return "image";

  const extension = fileExtension(displayName);
  if (extension === "pdf") return "pdf";
  if (IMAGE_EXTENSIONS.has(extension)) return "image";
  return "unsupported";
}

export function sourcePreviewMimeType(displayName: string, kind?: SourcePreviewKind): string {
  const resolvedKind = kind ?? inferSourcePreviewKind(displayName);
  if (resolvedKind === "pdf") return "application/pdf";
  if (resolvedKind !== "image") return "application/octet-stream";

  switch (fileExtension(displayName)) {
    case "bmp":
      return "image/bmp";
    case "gif":
      return "image/gif";
    case "jpg":
    case "jpeg":
      return "image/jpeg";
    case "webp":
      return "image/webp";
    default:
      return "image/png";
  }
}

function fileExtension(displayName: string): string {
  return displayName.split(".").pop()?.trim().toLowerCase() ?? "";
}

function baseDescriptor(
  scope: "problem" | "submission",
  sourceId: string,
  displayName: string,
  previewKind: SourcePreviewKind,
): SourceFileDescriptor {
  return {
    file_id: `mock:${scope}:${sourceId || "unknown"}`,
    display_name: displayName,
    mime_type: sourcePreviewMimeType(displayName, previewKind),
    status: "available",
    preview_kind: previewKind,
    unavailable_reason: null,
  };
}
