import type { SourceFileDescriptor, SourcePreviewErrorCode } from "@/types/sourcePreview";

export const SOURCE_PREVIEW_NOT_CONNECTED = "source_preview_not_connected" as const;

export class SourcePreviewAdapterError extends Error {
  readonly code = SOURCE_PREVIEW_NOT_CONNECTED;

  constructor() {
    super(SOURCE_PREVIEW_NOT_CONNECTED);
    this.name = "SourcePreviewAdapterError";
  }
}

/**
 * Formal source-file download adapter boundary.
 *
 * LYJ has not frozen the owner-scoped binary route yet, so this function must
 * not guess a URL. Replace only this adapter after the backend contract lands.
 */
export function loadSourcePreviewFile(
  _descriptor: SourceFileDescriptor | null,
): Promise<Blob> {
  return Promise.reject(new SourcePreviewAdapterError());
}

export function sourcePreviewErrorCode(error: unknown): SourcePreviewErrorCode {
  return error instanceof SourcePreviewAdapterError
    ? error.code
    : "source_preview_load_failed";
}
