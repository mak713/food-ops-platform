// New for Phase 6 (Final Plan §I) — extracts the inline stale-version-conflict panel
// that was hand-duplicated across CustomerFormPage.tsx/CustomerDetailPage.tsx/
// ProductDetailPage.tsx/SellingOptionRow.tsx. Orders needs this panel in three places
// at once (update/delete/add-payment), which is what tips this over from "acceptable
// duplication" into "worth extracting" — existing Phase 3-5 call sites are left
// untouched (no retrofit, Final Pre-Implementation Amendment §20).

import { Button } from "../ui/button";

interface StaleVersionPanelProps {
  onRefresh: () => void;
}

export function StaleVersionPanel({ onRefresh }: StaleVersionPanelProps) {
  return (
    <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
      <p role="alert">
        This record changed since you opened it. Refresh the latest version and review
        your changes before saving again.
      </p>
      <Button type="button" variant="outline" size="sm" onClick={onRefresh}>
        Refresh
      </Button>
    </div>
  );
}
