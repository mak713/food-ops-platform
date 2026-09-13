// Typed calls to the Phase 5 Purchased Product Inventory endpoints
// (backend/app/api/v1/purchased_inventory.py) — both the Product-nested mutation/read
// routes and the top-level `/api/v1/purchased-inventory` list that powers the
// Inventory-module list screen (Phase 5 Plan §F). Mirrors backend/app/schemas/inventory.py.

import { apiFetch } from "../../api/client";
import type { PageResponse } from "../customers/api";
import type { InventoryTransaction, ManualAdjustmentReason } from "../ingredients/inventoryApi";

export interface PurchasedProductInventory {
  product_id: string;
  physical_quantity: string;
  weighted_average_unit_cost: string;
  latest_purchase_unit_cost: string | null;
  replacement_unit_cost: string | null;
  effective_replacement_cost: string | null;
  version: number;
}

export interface PurchasedProductInventorySummary {
  product_id: string;
  product_name: string;
  product_is_active: boolean;
  physical_quantity: string | null;
  weighted_average_unit_cost: string | null;
  latest_purchase_unit_cost: string | null;
  replacement_unit_cost: string | null;
  effective_replacement_cost: string | null;
  version: number | null;
}

export interface PurchasedInitialBalanceRequest {
  quantity: string;
  unit_cost: string;
  supplier_text?: string | null;
  notes?: string | null;
}

export interface PurchasedRestockRequest {
  // Omitted/undefined asserts "I believe this product's inventory has never been
  // initialized yet" — a legitimate first-ever purchase event (Phase 5 Plan §C/§E).
  version?: number;
  quantity: string;
  unit_cost: string;
  supplier_text?: string | null;
  notes?: string | null;
}

export interface PurchasedAdjustmentRequest {
  version: number;
  quantity_change: string;
  reason: ManualAdjustmentReason;
  notes?: string | null;
}

export interface ReplacementCostRequest {
  version: number;
  replacement_unit_cost: string | null;
}

export interface PurchasedInventoryListParams {
  is_active?: boolean;
  limit?: number;
  offset?: number;
}

export interface TransactionListParams {
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

function put<T>(path: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, { method: "PUT", body: JSON.stringify(body) });
}

export const purchasedInventoryApi = {
  get: (productId: string) =>
    apiFetch<PurchasedProductInventory>(`/api/v1/products/${productId}/purchased-inventory`),
  createInitialBalance: (productId: string, payload: PurchasedInitialBalanceRequest) =>
    post<PurchasedProductInventory>(
      `/api/v1/products/${productId}/purchased-inventory/initial-balance`,
      payload,
    ),
  restock: (productId: string, payload: PurchasedRestockRequest) =>
    post<PurchasedProductInventory>(
      `/api/v1/products/${productId}/purchased-inventory/restock`,
      payload,
    ),
  adjust: (productId: string, payload: PurchasedAdjustmentRequest) =>
    post<PurchasedProductInventory>(
      `/api/v1/products/${productId}/purchased-inventory/adjustments`,
      payload,
    ),
  setReplacementCost: (productId: string, payload: ReplacementCostRequest) =>
    put<PurchasedProductInventory>(
      `/api/v1/products/${productId}/purchased-inventory/replacement-cost`,
      payload,
    ),
  listTransactions: (productId: string, params: TransactionListParams = {}) =>
    apiFetch<PageResponse<InventoryTransaction>>(
      `/api/v1/products/${productId}/purchased-inventory/transactions${buildQuery(params)}`,
    ),
  listForBusiness: (params: PurchasedInventoryListParams = {}) =>
    apiFetch<PageResponse<PurchasedProductInventorySummary>>(
      `/api/v1/purchased-inventory${buildQuery(params)}`,
    ),
  /** Every PURCHASED product regardless of active state, across all pages — used by the
   * Inventory History picker (Phase 5 Plan §F), which must still reach an inactive
   * product's history. */
  listAll: async (): Promise<PurchasedProductInventorySummary[]> => {
    const all: PurchasedProductInventorySummary[] = [];
    let offset = 0;
    const limit = 200;
    for (;;) {
      const page = await purchasedInventoryApi.listForBusiness({ limit, offset });
      all.push(...page.items);
      if (all.length >= page.total || page.items.length < limit) break;
      offset += limit;
    }
    return all;
  },
};
