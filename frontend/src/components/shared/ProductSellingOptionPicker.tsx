// New for Phase 6 (Final Plan §I) — a Product -> Selling Option cascading picker for
// STANDARD_OPTION order lines. No combobox/typeahead exists anywhere in the app yet
// (Products/Ingredients all use a plain <Select> over an already-known option set); the
// nearest existing analog is lib/units.ts's static family-filtered dropdown. This is the
// data-driven equivalent: choosing a Product refetches that Product's own (embedded)
// Selling Options and resets the Selling Option choice.

import { useQuery } from "@tanstack/react-query";
import { productsApi } from "../../features/products/api";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../ui/select";

interface ProductSellingOptionPickerProps {
  productId: string | null;
  sellingOptionId: string | null;
  onChange: (productId: string | null, sellingOptionId: string | null) => void;
  // Stored OrderLine identity to show as interim/fallback context (Final Hardening §2)
  // while the live Product detail is loading — this is the line's own already-known
  // snapshot, never fabricated, so it's safe to show before the catalog fetch resolves.
  fallbackDisplayName?: string;
  // Phase 7 Final Remediation Correction Plan, Finding 3 — a production-locked
  // STANDARD_OPTION line's source (Product/Selling Option) must not be newly
  // selectable, while non-production fields on the same line stay editable. Combined
  // (via `||`) with each Select's own existing loading/empty-state disable logic
  // below, never replacing it.
  disabled?: boolean;
}

export function ProductSellingOptionPicker({
  productId,
  sellingOptionId,
  onChange,
  fallbackDisplayName,
  disabled = false,
}: ProductSellingOptionPickerProps) {
  const productsQuery = useQuery({
    queryKey: ["products", { is_active: true, limit: 200 }],
    queryFn: () => productsApi.list({ is_active: true, limit: 200 }),
  });

  const productDetailQuery = useQuery({
    queryKey: ["products", productId],
    queryFn: () => productsApi.get(productId as string),
    enabled: Boolean(productId),
  });

  // Eligibility for a *new* Standard Option source selection (Manual Acceptance
  // Pricing/UX Correction §1): a Product with zero active Selling Options cannot
  // actually form a valid Standard Option line (the backend requires a Selling
  // Option), so it must not be newly choosable here — CUSTOM_QUANTITY has no such
  // restriction and intentionally continues to offer every active Product (see
  // OrderLineRow.tsx's own, unfiltered Product list for that line type). `allActiveProducts`
  // is kept separately from the filtered `eligibleProducts` because the carried-forward
  // display logic below still needs to know whether the *current* Product is active at
  // all, independent of whether it's eligible for a *new* pick.
  const allActiveProducts = productsQuery.data?.items ?? [];
  const eligibleProducts = allActiveProducts.filter((p) => p.has_active_selling_option);
  const isCurrentProductInEligibleList = eligibleProducts.some((p) => p.id === productId);
  const currentProductIsInactive =
    Boolean(productId) && productDetailQuery.data?.id === productId && !productDetailQuery.data.is_active;

  // Carried-forward reference display (Final Hardening §2 / ADR-108): the retained
  // Selling Option must stay visible even though it's excluded from "new" choices below,
  // and only OTHER archived options are actually hidden from the offered list — an
  // inactive Product's own archived Selling Options were previously all offered
  // indiscriminately, which is what this filter closes. The archived-current option is
  // explicitly placed LAST (never interleaved at its natural sort_order position): if it
  // sorted ahead of whichever option the seller just picked, its removal from this list —
  // which happens the instant the seller picks a *different* option, since it then stops
  // matching `sellingOptionId` — would shift that just-picked option's base-ui item index
  // out from under the click still resolving, and base-ui's own selected-value/registered-
  // items consistency check would reset the selection back to null (reproduced directly
  // against base-ui's SelectPositioner `onMapChange`). Appending it last means removing it
  // can only ever affect indices after the option just picked, never before.
  const activeSellingOptions = (productDetailQuery.data?.selling_options ?? []).filter(
    (option) => option.is_active,
  );
  const archivedCurrentSellingOption = (productDetailQuery.data?.selling_options ?? []).find(
    (option) => !option.is_active && option.id === sellingOptionId,
  );
  const sellingOptions = archivedCurrentSellingOption
    ? [...activeSellingOptions, archivedCurrentSellingOption]
    : activeSellingOptions;

  // A selected, active Product with literally zero active Selling Options (and no
  // archived-current one carried forward) leaves the seller looking at a disabled,
  // unexplained selector — surface this explicitly rather than leaving it ambiguous
  // (Final Manual Acceptance §4). The Zod line schema already requires a non-empty
  // `selling_option_id` for STANDARD_OPTION, so this state already blocks submission;
  // this message only explains *why* nothing is selectable.
  //
  // Gated on the Product detail query having actually *succeeded* for the currently
  // selected Product (Select Fix Final Micro-Hardening §2) — `sellingOptions` is
  // trivially empty immediately after picking a Product, before its detail query has
  // resolved, and the old `Boolean(productId) && ... && sellingOptions.length === 0`
  // condition couldn't distinguish that transient loading state from a genuine
  // zero-options Product, so it could flash the warning while data was still in
  // flight. `productDetailQuery.isSuccess` is false during loading, before the query
  // has ever run, and on a fetch error — so all three of those are excluded here
  // without inventing any new error-surfacing UI for this micro-pass (this component
  // has none today; a fetch error simply leaves the message unshown, matching how it
  // already leaves the Selling Option Select showing "Loading…"/disabled).
  const productDetailLoadedForCurrentSelection =
    Boolean(productId) &&
    productDetailQuery.isSuccess &&
    productDetailQuery.data?.id === productId;
  const hasNoActiveSellingOptions =
    productDetailLoadedForCurrentSelection &&
    !currentProductIsInactive &&
    sellingOptions.length === 0;
  // Selling Option query error — distinct from "zero active options" (End-to-End Fix
  // §7): an error means the options couldn't be determined at all, not that this
  // Product genuinely has none. Only surfaced once a Product is actually selected —
  // there is nothing to have failed to load before then.
  const sellingOptionLoadError = Boolean(productId) && productDetailQuery.isError;

  // Loading / error / empty-set states for the Product control itself must stay
  // distinguishable (End-to-End Fix §7) — this filtering already once caused the whole
  // seller workflow to look "broken" when the real root cause was a stale dev-server
  // response, not the filter logic; explicit states make a genuine future failure mode
  // (query still loading, or errored) visually distinct from an honest zero-eligible
  // result instead of all three silently looking like an unresponsive/empty control.
  const productsIsLoading = productsQuery.isLoading;
  const productsIsError = productsQuery.isError;
  // "Nothing at all to select" means no *new* eligible Product AND no carried-forward
  // current reference either (ADR-108) — a carried-forward-only Product still gives the
  // seller one real, meaningful item in the list, so that case is not "empty."
  const hasAnySelectableProduct =
    eligibleProducts.length > 0 ||
    (Boolean(productId) && !isCurrentProductInEligibleList && Boolean(productDetailQuery.data));
  const productsHaveNoEligibleOptions = productsQuery.isSuccess && !hasAnySelectableProduct;

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col gap-2 sm:flex-row">
        <Select
          // Keyed by the current value — see the matching comment on the Selling
          // Option Select below for why: picking a different Product must fully
          // remount this Select rather than reactively unmounting the
          // archived-current SelectItem under an already-mounted instance, which is
          // what triggers base-ui's registered-items consistency check to reset the
          // just-made selection to null (reproduced directly against
          // SelectPositioner `onMapChange`). The key is namespaced with a
          // role-specific prefix UNCONDITIONALLY, not only in the empty-value
          // fallback (Select Fix Final Micro-Hardening §1) — the manual-acceptance
          // defect was two sibling Selects sharing an identical key, and an
          // empty-only fallback (`productId || "product-none"`) only guarantees
          // uniqueness for the *unset* case; a Product ID and a Selling Option ID
          // are values from entirely separate tables, and while a real UUID
          // collision between them is astronomically unlikely, sibling React
          // identity should not structurally depend on that. Prefixing every value
          // (`product-${productId ?? "none"}`) makes the two Selects' keys disjoint
          // by construction, for every possible value, not just when both are
          // empty. This is the same duplicate-key error confirmed via the console
          // warning "Encountered two children with the same key" during real-
          // browser manual acceptance testing — the fix is what makes the Selects
          // safe to remount independently; the remount-by-key strategy itself was
          // investigated and found not to be the actual defect.
          key={`product-${productId ?? "none"}`}
          value={productId ?? ""}
          onValueChange={(value) => onChange(value || null, null)}
          // Disabled only while genuinely loading, or once loaded with truly nothing
          // selectable (End-to-End Fix §7) — never disabled merely because the request
          // is in flight for a normal, soon-to-resolve reason, and never disabled on an
          // error (the seller should still be able to see/retry, not be silently locked
          // out indistinguishably from "zero eligible").
          disabled={disabled || productsIsLoading || productsHaveNoEligibleOptions}
        >
          <SelectTrigger aria-label="Product">
            {/* Render-prop, not a plain `placeholder` — an ineligible carried-forward
                Product (archived, or active-but-optionless — Manual Acceptance
                Pricing/UX Correction §1) is absent from `eligibleProducts` by
                construction (only eligible Products are ever offered as new
                choices), so without this the trigger would show a blank/raw id
                instead of the Product's real name. */}
            <SelectValue>
              {(value: string) => {
                if (productsIsLoading) return "Loading products…";
                if (!value) return "Select a product";
                const eligibleMatch = eligibleProducts.find((p) => p.id === value);
                if (eligibleMatch) return eligibleMatch.name;
                if (productDetailQuery.data && productDetailQuery.data.id === value) {
                  if (!productDetailQuery.data.is_active) {
                    return `${productDetailQuery.data.name} (archived)`;
                  }
                  // Active, but absent from `eligibleProducts` — it has zero active
                  // Selling Options, not that it's archived; label it distinctly so
                  // the seller isn't told the wrong reason it can't be freshly
                  // reselected.
                  return `${productDetailQuery.data.name} (no active options)`;
                }
                return fallbackDisplayName ?? "Loading…";
              }}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {eligibleProducts.map((product) => (
              <SelectItem key={product.id} value={product.id}>
                {product.name}
              </SelectItem>
            ))}
            {/* The currently-attached Product, only when it's ineligible for a *new*
                pick — either genuinely archived, or active but with zero active
                Selling Options (Manual Acceptance Pricing/UX Correction §1) — so
                it's missing from `eligibleProducts` above. Visible/preserved as
                "leave unchanged" (ADR-108), never offered once the seller picks a
                different (necessarily eligible) Product. Rendered LAST — see the
                matching comment on `sellingOptions` below for why order matters
                here. */}
            {productId && !isCurrentProductInEligibleList && productDetailQuery.data && (
              <SelectItem value={productId}>
                {productDetailQuery.data.name}{" "}
                {productDetailQuery.data.is_active
                  ? "(no active options — current)"
                  : "(archived — current)"}
              </SelectItem>
            )}
          </SelectContent>
        </Select>

        <Select
          // Keyed by the current value: picking a genuinely different Selling
          // Option removes the archived-current option (above) from this list in
          // the very same commit that also changes `value` — under an
          // already-mounted base-ui Select instance, that combination hits a real
          // base-ui bug where the just-made selection gets silently reset back to
          // null (its registered-items consistency effect reads a `value` ref that
          // hasn't caught up to the fresh selection yet; reproduced directly
          // against SelectPositioner `onMapChange`). A remounted instance has no
          // stale "previous" state to reconcile against. Unconditionally namespaced
          // (not only in its empty-value fallback) so this Select's key can never
          // collide with the Product Select's above, for any value — see its
          // comment (Select Fix Final Micro-Hardening §1).
          key={`selling-option-${sellingOptionId ?? "none"}`}
          value={sellingOptionId ?? ""}
          onValueChange={(value) => onChange(productId, value || null)}
          // An inactive current Product may never have a *new* Selling Option
          // chosen under it — the seller must switch to an active Product first
          // (Final Hardening §2); the already-selected option (if any) stays
          // displayed above.
          disabled={
            disabled || !productId || sellingOptions.length === 0 || currentProductIsInactive
          }
        >
          <SelectTrigger aria-label="Selling option">
            <SelectValue>
              {(value: string) => {
                if (productId && productDetailQuery.isLoading) return "Loading options…";
                if (!value) return "Select an option";
                const match = sellingOptions.find((o) => o.id === value);
                if (match) return `${match.name}${!match.is_active ? " (archived)" : ""}`;
                return fallbackDisplayName ?? "Loading…";
              }}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {sellingOptions.map((option) => (
              <SelectItem key={option.id} value={option.id}>
                {option.name}
                {!option.is_active ? " (archived)" : ""}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {productsIsError && (
        <p className="text-xs text-destructive" role="alert">
          Unable to load products. Try again.
        </p>
      )}
      {productsHaveNoEligibleOptions && !productsIsError && (
        <p className="text-xs text-muted-foreground" role="status">
          No products with active selling options are available.
        </p>
      )}
      {sellingOptionLoadError && (
        <p className="text-xs text-destructive" role="alert">
          Unable to load this product's selling options. Try again.
        </p>
      )}
      {hasNoActiveSellingOptions && (
        <p className="text-xs text-destructive" role="alert">
          No active selling options available for this product.
        </p>
      )}
    </div>
  );
}
