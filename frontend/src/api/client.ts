// Minimal API client foundation (Spec §12.4). Kept thin in Phase 0: no
// auth-expiry handling yet (needs sessions — Phase 2). Requests go through
// Vite's dev-server proxy (see vite.config.ts) so no base URL is needed
// locally; VITE_API_BASE_URL is only read where that proxy isn't in play.

export interface ApiErrorIssue {
  severity: "ERROR" | "WARNING" | "NOTICE";
  code: string;
  message: string;
  field: string | null;
  resource: string | null;
  details: Record<string, unknown>;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    issues: ApiErrorIssue[];
    request_id?: string;
  };
}

export class ApiError extends Error {
  status: number;
  body: ApiErrorBody | null;

  constructor(message: string, status: number, body: ApiErrorBody | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "";

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });

  if (!response.ok) {
    let body: ApiErrorBody | null = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    throw new ApiError(body?.error.message ?? response.statusText, response.status, body);
  }

  return response.json() as Promise<T>;
}
