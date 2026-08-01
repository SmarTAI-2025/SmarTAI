import axios, { AxiosError, type AxiosProgressEvent, type AxiosRequestConfig } from "axios";

export const SMARTAI_TOKEN_STORAGE_KEY = "smartai_token";

const DEFAULT_BACKEND_URL = "http://localhost:8000";

export const backendUrl = (
  import.meta.env.VITE_SMARTAI_BACKEND_URL?.trim() || DEFAULT_BACKEND_URL
).replace(/\/+$/, "");

export interface APIErrorPayload {
  error?: unknown;
  detail?: unknown;
  message?: unknown;
  raw?: unknown;
  [key: string]: unknown;
}

export class APIError extends Error {
  readonly status: number;
  readonly payload?: APIErrorPayload;
  readonly retryAfterSeconds?: number;

  constructor(
    status: number,
    message: string,
    payload?: APIErrorPayload,
    retryAfterSeconds?: number,
  ) {
    super(message);
    this.name = "APIError";
    this.status = status;
    this.payload = payload;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export const apiClient = axios.create({
  baseURL: backendUrl,
  timeout: 30_000,
  withCredentials: true,
  headers: {
    Accept: "application/json",
  },
});

interface RefreshableRequestConfig extends AxiosRequestConfig {
  _retry?: boolean;
  _skipAuthRefresh?: boolean;
}

let refreshPromise: Promise<string> | null = null;

apiClient.interceptors.request.use((config) => {
  const token = getAuthToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as RefreshableRequestConfig | undefined;
    const path = config?.url ?? "";
    if (error.response?.status !== 401 || !config || config._retry || config._skipAuthRefresh || path.includes("/auth/login") || path.includes("/auth/refresh")) {
      return Promise.reject(error);
    }
    config._retry = true;
    refreshPromise ??= apiClient
      .post<{ token: string }>("/auth/refresh", {}, { _skipAuthRefresh: true } as RefreshableRequestConfig)
      .then((response) => {
        setAuthToken(response.data.token);
        return response.data.token;
      })
      .finally(() => {
        refreshPromise = null;
      });
    try {
      const token = await refreshPromise;
      config.headers = { ...config.headers, Authorization: `Bearer ${token}` };
      return apiClient.request(config);
    } catch (refreshError) {
      clearAuthToken();
      return Promise.reject(refreshError);
    }
  },
);

export function getAuthToken(): string | null {
  if (typeof localStorage === "undefined") {
    return null;
  }
  return localStorage.getItem(SMARTAI_TOKEN_STORAGE_KEY);
}

export function setAuthToken(token: string): void {
  localStorage.setItem(SMARTAI_TOKEN_STORAGE_KEY, token);
}

export function clearAuthToken(): void {
  localStorage.removeItem(SMARTAI_TOKEN_STORAGE_KEY);
}

export function normalizeAPIError(error: unknown): APIError {
  if (error instanceof APIError) {
    return error;
  }

  if (axios.isAxiosError(error)) {
    return normalizeAxiosError(error);
  }

  if (error instanceof Error) {
    return new APIError(0, error.message);
  }

  if (typeof error === "string" && error.trim()) {
    return new APIError(0, error.trim());
  }

  return new APIError(0, "Unknown API error");
}

export function getAPIErrorDetail(error: unknown): Record<string, unknown> | null {
  const payload = normalizeAPIError(error).payload;
  return errorRecords(payload)[0] ?? null;
}

export function getAPIErrorCode(error: unknown): string | null {
  const payload = normalizeAPIError(error).payload;
  for (const record of errorRecords(payload)) {
    const value = record.code;
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
  }
  return null;
}

export async function getJSON<T>(path: string, config?: AxiosRequestConfig): Promise<T> {
  try {
    const response = await apiClient.get<T>(path, config);
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

export async function getBlob(path: string, config?: AxiosRequestConfig): Promise<Blob> {
  try {
    const response = await apiClient.get<Blob>(path, { ...config, responseType: "blob" });
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

export async function postJSON<TResponse, TBody = unknown>(
  path: string,
  body?: TBody,
  config?: AxiosRequestConfig,
): Promise<TResponse> {
  try {
    const response = await apiClient.post<TResponse>(path, body ?? {}, config);
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

export async function putJSON<TResponse, TBody = unknown>(
  path: string,
  body?: TBody,
  config?: AxiosRequestConfig,
): Promise<TResponse> {
  try {
    const response = await apiClient.put<TResponse>(path, body ?? {}, config);
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

export async function deleteJSON<T>(path: string, config?: AxiosRequestConfig): Promise<T> {
  try {
    const response = await apiClient.delete<T>(path, config);
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

export interface UploadOptions {
  contentType?: string;
  onProgress?: (percent: number, event: AxiosProgressEvent) => void;
  fields?: Record<string, string | number | boolean | null | undefined>;
  files?: Record<string, File | null | undefined>;
}

export async function postMultipart<T>(
  path: string,
  file: File | null,
  options: UploadOptions = {},
): Promise<T> {
  const formData = new FormData();
  if (file) {
    formData.append("file", file);
  }
  Object.entries(options.fields ?? {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined) {
      formData.append(key, String(value));
    }
  });
  Object.entries(options.files ?? {}).forEach(([key, value]) => {
    if (value) {
      formData.append(key, value);
    }
  });

  try {
    const response = await apiClient.post<T>(path, formData, {
      timeout: 180_000,
      headers: options.contentType ? { "Content-Type": options.contentType } : undefined,
      onUploadProgress: (event) => {
        if (!options.onProgress) {
          return;
        }
        const percent = event.total ? Math.round((event.loaded / event.total) * 100) : 0;
        options.onProgress(percent, event);
      },
    });
    return response.data;
  } catch (error) {
    throw normalizeAPIError(error);
  }
}

function normalizeAxiosError(error: AxiosError): APIError {
  const status = error.response?.status ?? 0;
  const payload = normalizePayload(error.response?.data);
  const message = error.response
    ? responseMessage(status, payload, error.response.statusText)
    : networkMessage(error);
  const retryAfterSeconds = parseRetryAfter(
    error.response?.headers?.["retry-after"],
    payload,
  );

  return new APIError(status, message, payload, retryAfterSeconds);
}

function normalizePayload(data: unknown): APIErrorPayload | undefined {
  if (data && typeof data === "object") {
    return data as APIErrorPayload;
  }
  if (typeof data === "string" && data) {
    return { raw: data };
  }
  return undefined;
}

function responseMessage(status: number, payload: APIErrorPayload | undefined, fallback: string): string {
  for (const candidate of [payload?.error, payload?.detail, payload?.message, payload?.raw]) {
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
    if (Array.isArray(candidate)) {
      return candidate
        .map((item) => {
          if (item && typeof item === "object" && "msg" in item) {
            return String((item as { msg: unknown }).msg);
          }
          return String(item);
        })
        .join("; ");
    }
    const record = asRecord(candidate);
    if (record) {
      const nestedMessage = record.message ?? record.reason ?? record.error;
      if (typeof nestedMessage === "string" && nestedMessage.trim()) {
        return nestedMessage.trim();
      }
      if (typeof record.code === "string" && record.code.trim()) {
        return record.code.trim();
      }
    }
  }
  if (typeof payload?.code === "string" && payload.code.trim()) {
    return payload.code.trim();
  }
  return fallback || `Request failed with status ${status}`;
}

function parseRetryAfter(value: unknown, payload?: APIErrorPayload): number | undefined {
  const records = errorRecords(payload);
  const candidate = value
    ?? records.map((record) => record.retry_after_seconds ?? record.retry_after)
      .find((item) => item !== null && item !== undefined);
  if (typeof candidate === "number" && Number.isFinite(candidate) && candidate >= 0) {
    return Math.ceil(candidate);
  }
  if (typeof candidate !== "string" || !candidate.trim()) return undefined;
  const seconds = Number(candidate);
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds);
  const timestamp = Date.parse(candidate);
  if (!Number.isFinite(timestamp)) return undefined;
  return Math.max(0, Math.ceil((timestamp - Date.now()) / 1000));
}

function errorRecords(payload?: APIErrorPayload): Record<string, unknown>[] {
  if (!payload) return [];
  const records = [payload.error, payload.detail, payload]
    .map(asRecord)
    .filter((record): record is Record<string, unknown> => record !== null);
  return records.filter((record, index) => records.indexOf(record) === index);
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function networkMessage(error: AxiosError): string {
  if (error.code === "ECONNABORTED") {
    return "Request timed out. The backend may still be waking up.";
  }
  return error.message || "Network error";
}
