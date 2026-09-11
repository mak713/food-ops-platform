// Typed calls to the Phase 3 Customer endpoints (backend/app/api/v1/customers.py).
// Mirrors backend/app/schemas/customer.py's request/response shapes (established
// hand-maintained-mirror convention — see features/auth/api.ts).

import { apiFetch } from "../../api/client";

export interface Customer {
  id: string;
  name: string;
  phone: string | null;
  email: string | null;
  preferred_contact_method: string | null;
  notes: string | null;
  is_active: boolean;
  version: number;
}

export interface CustomerSummary {
  id: string;
  name: string;
  phone: string | null;
  email: string | null;
  is_active: boolean;
  version: number;
}

export interface PageResponse<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface CustomerCreateRequest {
  name: string;
  phone?: string | null;
  email?: string | null;
  preferred_contact_method?: string | null;
  notes?: string | null;
  confirm_duplicate?: boolean;
}

export interface CustomerUpdateRequest {
  version: number;
  name?: string;
  phone?: string | null;
  email?: string | null;
  preferred_contact_method?: string | null;
  notes?: string | null;
}

export interface CustomerListParams {
  q?: string;
  is_active?: boolean;
  limit?: number;
  offset?: number;
}

function buildQuery(params: object): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

function post<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: "POST", body: JSON.stringify(body) });
}

function patch<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: "PATCH", body: JSON.stringify(body) });
}

export const customersApi = {
  list: (params: CustomerListParams = {}) =>
    apiFetch<PageResponse<CustomerSummary>>(`/api/v1/customers${buildQuery(params)}`),
  get: (id: string) => apiFetch<Customer>(`/api/v1/customers/${id}`),
  create: (payload: CustomerCreateRequest) => post<Customer>("/api/v1/customers", payload),
  update: (id: string, payload: CustomerUpdateRequest) =>
    patch<Customer>(`/api/v1/customers/${id}`, payload),
  archive: (id: string, version: number) =>
    post<Customer>(`/api/v1/customers/${id}/archive`, { version }),
  reactivate: (id: string, version: number) =>
    post<Customer>(`/api/v1/customers/${id}/reactivate`, { version }),
  remove: (id: string, version: number) =>
    apiFetch<void>(`/api/v1/customers/${id}?version=${version}`, { method: "DELETE" }),
};
