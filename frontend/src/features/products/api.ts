// Typed calls to the Phase 3 Product/SellingOption endpoints
// (backend/app/api/v1/products.py). Mirrors backend/app/schemas/product.py.
// ProductCreateRequest has no selling_options field — Selling Options are always
// created afterward via their own endpoints (Phase 3 plan v3 §9).

import { apiFetch } from "../../api/client";
import type { PageResponse } from "../customers/api";

export type ProductType = "PRODUCED" | "PURCHASED";

export interface SellingOption {
  id: string;
  product_id: string;
  name: string;
  quantity_units: string;
  price: string;
  packaging_cost: string;
  sort_order: number;
  is_active: boolean;
  version: number;
}

export interface Product {
  id: string;
  name: string;
  description: string | null;
  product_type: ProductType;
  default_packaging_cost: string;
  can_reuse_surplus: boolean;
  default_surplus_usable_days: number | null;
  is_active: boolean;
  version: number;
  selling_options: SellingOption[];
}

export interface ProductSummary {
  id: string;
  name: string;
  product_type: ProductType;
  is_active: boolean;
  version: number;
}

export interface ProductCreateRequest {
  name: string;
  product_type: ProductType;
  description?: string | null;
  default_packaging_cost?: string;
  can_reuse_surplus?: boolean;
  default_surplus_usable_days?: number | null;
}

export interface ProductUpdateRequest {
  version: number;
  name?: string;
  description?: string | null;
  default_packaging_cost?: string;
  can_reuse_surplus?: boolean;
  default_surplus_usable_days?: number | null;
}

export interface SellingOptionCreateRequest {
  name: string;
  quantity_units: string;
  price: string;
  packaging_cost?: string;
  sort_order?: number;
}

export interface SellingOptionUpdateRequest {
  version: number;
  name?: string;
  quantity_units?: string;
  price?: string;
  packaging_cost?: string;
  sort_order?: number;
}

export interface ProductListParams {
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

export const productsApi = {
  list: (params: ProductListParams = {}) =>
    apiFetch<PageResponse<ProductSummary>>(`/api/v1/products${buildQuery(params)}`),
  get: (id: string) => apiFetch<Product>(`/api/v1/products/${id}`),
  create: (payload: ProductCreateRequest) => post<Product>("/api/v1/products", payload),
  update: (id: string, payload: ProductUpdateRequest) =>
    patch<Product>(`/api/v1/products/${id}`, payload),
  archive: (id: string, version: number) =>
    post<Product>(`/api/v1/products/${id}/archive`, { version }),
  reactivate: (id: string, version: number) =>
    post<Product>(`/api/v1/products/${id}/reactivate`, { version }),
  remove: (id: string, version: number) =>
    apiFetch<void>(`/api/v1/products/${id}?version=${version}`, { method: "DELETE" }),
};

export const sellingOptionsApi = {
  create: (productId: string, payload: SellingOptionCreateRequest) =>
    post<SellingOption>(`/api/v1/products/${productId}/selling-options`, payload),
  update: (productId: string, optionId: string, payload: SellingOptionUpdateRequest) =>
    patch<SellingOption>(
      `/api/v1/products/${productId}/selling-options/${optionId}`,
      payload,
    ),
  archive: (productId: string, optionId: string, version: number) =>
    post<SellingOption>(`/api/v1/products/${productId}/selling-options/${optionId}/archive`, {
      version,
    }),
  reactivate: (productId: string, optionId: string, version: number) =>
    post<SellingOption>(
      `/api/v1/products/${productId}/selling-options/${optionId}/reactivate`,
      { version },
    ),
  remove: (productId: string, optionId: string, version: number) =>
    apiFetch<void>(
      `/api/v1/products/${productId}/selling-options/${optionId}?version=${version}`,
      { method: "DELETE" },
    ),
};
