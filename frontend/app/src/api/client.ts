import axios, { AxiosError, type AxiosProgressEvent, type AxiosRequestConfig } from "axios";

export const SMARTAI_TOKEN_STORAGE_KEY = "smartai_token";

const DEFAULT_BACKEND_URL = "http://localhost:8000";

export const backendUrl = (
  import.meta.env.VITE_SMARTAI_BACKEND_URL?.trim() || DEFAULT_BACKEND_URL
).replace(/\/+$/, "");

export interface APIErrorPayload {
  detail?: unknown;
  message?: unknown;
  raw?: unknown;
  [key: string]: unknown;
}

export class APIError extends Error {
  readonly status: number;
  readonly payload?: APIErrorPayload;

  constructor(status: number, message: string, payload?: APIErrorPayload) {
    super(message);
    this.name = "APIError";
    this.status = status;
    this.payload = payload;
  }
}

export const apiClient = axios.create({
  baseURL: backendUrl,
  timeout: 30_000,
  headers: {
    Accept: "application/json",
  },
});

apiClient.interceptors.request.use((config) => {
  const token = getAuthToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

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

  return new APIError(0, "Unknown API error");
}

export async function getJSON<T>(path: string, config?: AxiosRequestConfig): Promise<T> {
  try {
    const response = await apiClient.get<T>(path, config);
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
}

export async function postMultipart<T>(
  path: string,
  file: File,
  options: UploadOptions = {},
): Promise<T> {
  const formData = new FormData();
  formData.append("file", file);

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

  return new APIError(status, message, payload);
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
  const detail = payload?.detail ?? payload?.message ?? payload?.raw;
  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          return String((item as { msg: unknown }).msg);
        }
        return String(item);
      })
      .join("; ");
  }
  return fallback || `Request failed with status ${status}`;
}

function networkMessage(error: AxiosError): string {
  if (error.code === "ECONNABORTED") {
    return "Request timed out. The backend may still be waking up.";
  }
  return error.message || "Network error";
}
