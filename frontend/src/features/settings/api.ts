// Typed calls to the Phase 11 Settings endpoints (backend/app/api/v1/settings.py). Mirrors
// backend/app/schemas/settings.py.
//
// Responses are validated strictly with zod: a body that is not a Business Settings document
// (a malformed/foreign payload, a proxy error page, an unrelated endpoint's shape) is an
// error, never partially rendered. `default_fulfillment_method` is intentionally not part of
// this contract (Phase 11 scope).

import { z } from "zod";
import { apiFetch } from "../../api/client";
import type { UserSummary } from "../auth/api";

const moneyString = z.string().regex(/^\d+\.\d{2}$/, "Expected a 2-decimal money string");
const ratioString = z.string().regex(/^0\.\d{6}$/, "Expected a 6-decimal ratio string");

export const businessSettingsSchema = z.object({
  id: z.string().min(1),
  name: z.string(),
  business_type: z.string().nullable(),
  contact_phone: z.string().nullable(),
  contact_email: z.string().nullable(),
  address_text: z.string().nullable(),
  timezone: z.string().min(1),
  default_labor_rate: moneyString,
  target_contribution_margin: ratioString.nullable(),
  fulfillment_warning_minutes: z.number().int(),
  shopping_horizon_days: z.number().int(),
  version: z.number().int(),
});
export type BusinessSettings = z.infer<typeof businessSettingsSchema>;

const ownerSchema = z.object({ id: z.string(), name: z.string(), email: z.string() });

/** Editable Business fields; `version` is sent separately. Absent = unchanged, null clears. */
export interface BusinessSettingsChanges {
  name?: string;
  business_type?: string | null;
  contact_phone?: string | null;
  contact_email?: string | null;
  address_text?: string | null;
  timezone?: string;
  default_labor_rate?: string;
  target_contribution_margin?: string | null;
  fulfillment_warning_minutes?: number;
  shopping_horizon_days?: number;
}

export class InvalidSettingsResponseError extends Error {
  constructor() {
    super("The server returned an unexpected Settings response.");
    this.name = "InvalidSettingsResponseError";
  }
}

function parseOrThrow<T>(schema: z.ZodType<T>, body: unknown): T {
  const result = schema.safeParse(body);
  if (!result.success) throw new InvalidSettingsResponseError();
  return result.data;
}

export const SETTINGS_QUERY_KEY = ["settings", "business"] as const;

export const settingsApi = {
  get: async (): Promise<BusinessSettings> =>
    parseOrThrow(businessSettingsSchema, await apiFetch<unknown>("/api/v1/settings")),

  updateBusiness: async (
    version: number,
    changes: BusinessSettingsChanges,
  ): Promise<BusinessSettings> =>
    parseOrThrow(
      businessSettingsSchema,
      await apiFetch<unknown>("/api/v1/settings/business", {
        method: "PATCH",
        body: JSON.stringify({ version, ...changes }),
      }),
    ),

  updateOwner: async (name: string): Promise<UserSummary> =>
    parseOrThrow(
      ownerSchema,
      await apiFetch<unknown>("/api/v1/settings/owner", {
        method: "PATCH",
        body: JSON.stringify({ name }),
      }),
    ),
};
