import type { SourcePreviewKind } from "@/types/sourcePreview";

const IMAGE_EXTENSIONS = new Set(["jpeg", "jpg", "png", "webp"]);

const IMAGE_MIME_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/webp",
]);

export function inferSourcePreviewKind(displayName: string, mimeType?: string | null): SourcePreviewKind {
  const normalizedMime = mimeType?.trim().toLowerCase() ?? "";
  if (normalizedMime === "application/pdf") return "pdf";
  if (IMAGE_MIME_TYPES.has(normalizedMime)) return "image";
  if (normalizedMime) return "unsupported";

  const extension = fileExtension(displayName);
  if (extension === "pdf") return "pdf";
  if (IMAGE_EXTENSIONS.has(extension)) return "image";
  return "unsupported";
}

function fileExtension(displayName: string): string {
  return displayName.split(".").pop()?.trim().toLowerCase() ?? "";
}
