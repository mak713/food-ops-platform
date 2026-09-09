// Typed calls to the Phase 2 auth endpoints (backend/app/api/v1/auth.py,
// backend/app/api/v1/account.py). Mirrors backend/app/schemas/auth.py's request shapes.

import { apiFetch } from "../../api/client";

export interface UserSummary {
  id: string;
  name: string;
  email: string;
}

export interface BusinessSummary {
  id: string;
  name: string;
  timezone: string;
}

export interface MeResponse {
  user: UserSummary;
  business: BusinessSummary;
}

export interface SignupRequest {
  name: string;
  email: string;
  password: string;
  business_name: string;
  business_timezone: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export interface DeleteAccountRequest {
  current_password: string;
  business_name_confirmation: string;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: "POST", body: JSON.stringify(body) });
}

export const authApi = {
  signup: (payload: SignupRequest) => post<MeResponse>("/api/v1/auth/signup", payload),
  login: (payload: LoginRequest) => post<MeResponse>("/api/v1/auth/login", payload),
  logout: () => post<{ status: string }>("/api/v1/auth/logout", {}),
  me: () => apiFetch<MeResponse>("/api/v1/auth/me"),
  changePassword: (payload: ChangePasswordRequest) =>
    post<{ status: string }>("/api/v1/auth/password/change", payload),
  requestPasswordReset: (email: string) =>
    post<{ status: string; message: string }>("/api/v1/auth/password/reset/request", { email }),
  confirmPasswordReset: (token: string, new_password: string) =>
    post<{ status: string }>("/api/v1/auth/password/reset/confirm", { token, new_password }),
  deleteAccount: (payload: DeleteAccountRequest) =>
    apiFetch<{ status: string }>("/api/v1/account", {
      method: "DELETE",
      body: JSON.stringify(payload),
    }),
};
