// Minimal API client foundation (Spec §12.4). Requests go through Vite's
// dev-server proxy (see vite.config.ts) so no base URL is needed locally;
// VITE_API_BASE_URL is only read where that proxy isn't in play.
//
// Phase 2: attaches the CSRF header to mutating requests (Phase 2 plan §3) by
// reading the non-HttpOnly `fo_csrf` cookie. This wrapper deliberately does
// NOT inspect response status codes to redirect on 401 — that would misfire
// on an ordinary wrong-password login attempt. The only place that treats a
// 401 as "session gone" is the auth context's current-user query (Phase 2
// plan §14) — see src/features/auth/AuthContext.tsx.
//
// Headers are built with the `Headers` API, not object spreading: `init?.headers`
// (typed `HeadersInit`) may be a plain object, a `Headers` instance, or an array of
// tuples, and only `new Headers(init?.headers)` handles all three uniformly — object
// spreading silently mishandled the latter two. The CSRF header is set (via
// `headers.set`, which replaces any existing value) *after* the caller's headers are
// loaded in, so a caller can never supply its own X-CSRF-Token and have it win over the
// real session-bound value (Checkpoint 2 review hardening).

import { getCookie } from "../lib/cookies";

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
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();

  // Start from whatever the caller passed — Headers() normalizes any of the three valid
  // HeadersInit shapes (plain object, Headers instance, tuple array) into one Headers
  // object, so caller headers are preserved regardless of which shape was used.
  const headers = new Headers(init?.headers);
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!SAFE_METHODS.has(method)) {
    const csrfToken = getCookie("fo_csrf");
    if (csrfToken) {
      // set() replaces any value the caller already supplied for this header — the
      // session-bound CSRF value always wins, never a caller-supplied one.
      headers.set("X-CSRF-Token", csrfToken);
    }
  }

  // `...init` is spread FIRST: if it came last, a caller-supplied `credentials` would
  // override "include" (Checkpoint 2 review, issue 4). Spreading init first, then
  // forcing `credentials` and the constructed `headers` afterward, means neither can be
  // overridden by a caller.
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    headers,
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

  // Phase 3 plan v3 §7/§18: DELETE returns a true 204 with no body — the first
  // empty-body response in the app (every Phase 2 response has a JSON body). Calling
  // `.json()` on an empty body throws, so this must be checked before attempting it.
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}
