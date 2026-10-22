// Shared plumbing for the three Business Settings forms (Business Profile, Costing & Pricing,
// Operations). Each form owns its own Save; they all edit the one Business Settings resource
// with optimistic concurrency, so the mutation, the field-error mapping and the stale-version
// recovery live here once.
//
// A form edits a *snapshot* (`base`) of the settings taken when it was loaded or last
// refreshed, and submits that snapshot's `version`. It deliberately does not follow later
// background refetches of the query: following them would silently re-base unsaved edits onto
// a newer version and defeat the stale-edit protection (Spec §8.33).

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FieldValues, Path, UseFormSetError } from "react-hook-form";
import { ApiError } from "../../api/client";
import { refreshTenantQueriesAfterTimezoneChange } from "../auth/tenantCache";
import { ME_QUERY_KEY } from "../auth/AuthContext";
import {
  type BusinessSettings,
  type BusinessSettingsChanges,
  SETTINGS_QUERY_KEY,
  settingsApi,
} from "./api";

export const nativeSelectClass =
  "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-base outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 md:text-sm dark:bg-input/30";

export function isStaleVersion(error: unknown): boolean {
  return error instanceof ApiError && error.body?.error.code === "STALE_VERSION";
}

export function errorMessage(error: unknown): string {
  return (
    (error instanceof ApiError ? error.body?.error.message : undefined) ??
    "Something went wrong. Please try again."
  );
}

/** Maps server validation issues (`issues[].field`) onto form fields that exist. */
export function applyServerFieldErrors<T extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<T>,
  fields: readonly string[],
): boolean {
  if (!(error instanceof ApiError) || error.status !== 422) return false;
  let applied = false;
  for (const issue of error.body?.error.issues ?? []) {
    if (issue.field && fields.includes(issue.field)) {
      setError(issue.field as Path<T>, { type: "server", message: issue.message });
      applied = true;
    }
  }
  return applied;
}

export function useBusinessSettingsSave(base: BusinessSettings) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (changes: BusinessSettingsChanges) =>
      settingsApi.updateBusiness(base.version, changes),
    onSuccess: (saved, changes) => {
      queryClient.setQueryData(SETTINGS_QUERY_KEY, saved);
      // The header (business name) and Order detail (timezone) read `me`.
      void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
      if (changes.timezone !== undefined) {
        // Nothing cached may keep data derived under the old timezone.
        refreshTenantQueriesAfterTimezoneChange(queryClient);
      }
    },
  });
}

/** Holds the settings snapshot a form edits and the version it submits. */
export function useSettingsBase(initial: BusinessSettings) {
  return useState<BusinessSettings>(initial);
}
