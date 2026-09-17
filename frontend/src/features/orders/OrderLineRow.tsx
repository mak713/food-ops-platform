// One Order Line row within OrderEntryPage's field array. Split into its own component
// (Checkpoint-3 correction 17) so it can use `useWatch` — a proper hook call scoped to
// this row's own `lines.${index}` path — instead of calling the parent form's `watch()`
// function imperatively inside a `.map()` callback, which is what triggered the React
// Compiler's "incompatible library" warning on the unsplit version. This also means an
// edit to one line no longer forces every other row to re-render.

import { useQuery } from "@tanstack/react-query";
import type { Control, UseFormRegister, UseFormSetValue } from "react-hook-form";
import { useWatch } from "react-hook-form";
import { CurrencyInput } from "../../components/shared/CurrencyInput";
import { FormField } from "../../components/shared/FormField";
import { ProductSellingOptionPicker } from "../../components/shared/ProductSellingOptionPicker";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import { Textarea } from "../../components/ui/textarea";
import { productsApi, type ProductSummary } from "../products/api";
import type { OrderLineType } from "./api";
import { emptyLine, type FormValues } from "./orderFormTypes";

interface OrderLineRowProps {
  control: Control<FormValues>;
  index: number;
  register: UseFormRegister<FormValues>;
  setValue: UseFormSetValue<FormValues>;
  remove: (index: number) => void;
  products: ProductSummary[];
  errorMessage?: string;
  // Phase 7 Final Remediation Correction Plan, Finding 3 — set only while this
  // Order is CONFIRMED and `production_locked`. Applies ONLY to the controls that
  // change Produced/Purchased demand identity or quantity (line-type Select,
  // Product/Selling-Option picker, `package_quantity`, `underlying_quantity` for
  // CUSTOM_QUANTITY, and Remove) — never to price, packaging, notes, or any
  // CUSTOM_ITEM field, which the Final Architecture Lock §C requires to stay
  // editable regardless of lock status.
  operationalDisabled?: boolean;
}

export function OrderLineRow({
  control,
  index,
  register,
  setValue,
  remove,
  products,
  errorMessage,
  operationalDisabled = false,
}: OrderLineRowProps) {
  const line = useWatch({ control, name: `lines.${index}` });
  const lineType = line?.line_type;

  // Carried-forward reference display for a CUSTOM_QUANTITY line's Product (Final
  // Hardening §2) — mirrors ProductSellingOptionPicker's own unconditional per-id
  // detail fetch, since the `products` prop (from OrderEntryPage) is active-only and
  // would otherwise have nothing to resolve an inactive carried-forward Product's name
  // against.
  const customQuantityProductDetailQuery = useQuery({
    queryKey: ["products", line?.product_id],
    queryFn: () => productsApi.get(line?.product_id as string),
    enabled: lineType === "CUSTOM_QUANTITY" && Boolean(line?.product_id),
  });
  const isCustomQuantityProductInActiveList = products.some((p) => p.id === line?.product_id);

  // The selected Selling Option's own name/quantity_units, so the Package quantity
  // field can spell out what a "package" actually is (Manual Acceptance Pricing/UX
  // Correction §3) — this manual tester understandably read "Package quantity" as an
  // individual-item count. Shares the exact `["products", productId]` cache key
  // `ProductSellingOptionPicker` already fetches under (no duplicate request).
  const standardOptionProductDetailQuery = useQuery({
    queryKey: ["products", line?.product_id],
    queryFn: () => productsApi.get(line?.product_id as string),
    enabled: lineType === "STANDARD_OPTION" && Boolean(line?.product_id),
  });
  const selectedSellingOption = standardOptionProductDetailQuery.data?.selling_options.find(
    (o) => o.id === line?.selling_option_id,
  );
  const packageQuantityNumber = Number(line?.package_quantity || 0);
  const underlyingUnitsPreview =
    selectedSellingOption && !Number.isNaN(packageQuantityNumber)
      ? Number((packageQuantityNumber * Number(selectedSellingOption.quantity_units)).toFixed(6))
      : null;

  return (
    <div className="flex flex-col gap-2 rounded-md border p-3">
      <div className="flex items-center justify-between">
        <Select
          value={lineType}
          onValueChange={(value) =>
            // Replaces the whole line object — a persisted line's `serverId` is
            // intentionally dropped here: the backend forbids changing an existing
            // line's type in place (ORDER_LINE_TYPE_IMMUTABLE), so switching type in
            // the UI is treated as "discard this line, start a fresh one of the new
            // type" — on save the old row is deleted and a new one is inserted. Marks
            // the form dirty (Final Hardening §1 BLOCKER) — this is exactly the kind
            // of meaningful controlled edit the unsaved-changes blocker exists for.
            setValue(`lines.${index}`, emptyLine(value as OrderLineType), { shouldDirty: true })
          }
          disabled={operationalDisabled}
        >
          <SelectTrigger aria-label={`Line ${index + 1} type`}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="STANDARD_OPTION">Standard Option</SelectItem>
            <SelectItem value="CUSTOM_QUANTITY">Custom Quantity</SelectItem>
            <SelectItem value="CUSTOM_ITEM">Custom Item</SelectItem>
          </SelectContent>
        </Select>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => remove(index)}
          disabled={operationalDisabled}
        >
          Remove
        </Button>
      </div>

      {lineType === "STANDARD_OPTION" && (
        <>
          <ProductSellingOptionPicker
            productId={line?.product_id || null}
            sellingOptionId={line?.selling_option_id || null}
            fallbackDisplayName={line?.display_name}
            disabled={operationalDisabled}
            onChange={(productId, sellingOptionId) => {
              // A genuine source-identity change (new id differs from what's stored),
              // not a same-id reselection — mirrors the backend's own
              // source_changed = product_changed OR option_changed rule. Marking dirty
              // is the Final Hardening §1 BLOCKER fix; clearing the stale override
              // reason is §4 — an old reason explaining a since-superseded override
              // must not be presented as though it still applies to the new source
              // (the backend itself clears it server-side under the same condition).
              const sourceChanged =
                (productId ?? "") !== (line?.product_id || "") ||
                (sellingOptionId ?? "") !== (line?.selling_option_id || "");
              setValue(`lines.${index}.product_id`, productId ?? undefined, {
                shouldDirty: true,
              });
              setValue(`lines.${index}.selling_option_id`, sellingOptionId ?? undefined, {
                shouldDirty: true,
              });
              if (sourceChanged && line?.price_override_reason) {
                setValue(`lines.${index}.price_override_reason`, "", { shouldDirty: true });
              }
            }}
          />
          <FormField
            id={`lines.${index}.package_quantity`}
            label={
              // Manual Acceptance Pricing/UX Correction §3 — "Package quantity" alone
              // reads like an individual-item count; naming the selected Selling
              // Option makes clear it's a count of *packages of that option*.
              selectedSellingOption
                ? `Package quantity — number of ${selectedSellingOption.name} packages`
                : "Package quantity"
            }
          >
            <Input
              id={`lines.${index}.package_quantity`}
              inputMode="decimal"
              disabled={operationalDisabled}
              {...register(`lines.${index}.package_quantity`)}
            />
          </FormField>
          {selectedSellingOption && underlyingUnitsPreview != null && (
            <p className="text-xs text-muted-foreground">
              {line?.package_quantity || 0} {selectedSellingOption.name}
              {packageQuantityNumber === 1 ? "" : "s"} = {underlyingUnitsPreview} underlying
              units.
            </p>
          )}
          <FormField
            id={`lines.${index}.charged_unit_price`}
            label="Price override (optional)"
          >
            <CurrencyInput
              id={`lines.${index}.charged_unit_price`}
              placeholder={
                line?.currentPriceSnapshot ? `Currently $${line.currentPriceSnapshot}` : undefined
              }
              {...register(`lines.${index}.charged_unit_price`)}
            />
          </FormField>
          {/* Wording depends on whether this is a persisted line (Final Hardening §3)
              — for a retained line, a blank override PRESERVES the stored snapshot; it
              does not silently refresh to the live catalog price unless the source
              itself changed. A new line has no stored snapshot, so the simpler
              catalog-price wording is accurate as-is. */}
          {line?.serverId ? (
            <p className="text-xs text-muted-foreground">
              {line?.currentPriceSnapshot
                ? `Stored price snapshot: $${line.currentPriceSnapshot}. `
                : ""}
              Leave the override blank to preserve this price. If you change the
              selling option, leaving the override blank uses the new option's current
              catalog price.
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              Leave blank to use the selling option's current catalog price.
            </p>
          )}
          <FormField
            id={`lines.${index}.price_override_reason`}
            label="Override reason (optional)"
          >
            <Input
              id={`lines.${index}.price_override_reason`}
              {...register(`lines.${index}.price_override_reason`)}
            />
          </FormField>
        </>
      )}

      {lineType === "CUSTOM_QUANTITY" && (
        <>
          <Select
            // Keyed by the current value so picking a different Product fully
            // remounts this Select rather than reactively unmounting the archived-
            // current SelectItem (below) under an already-mounted instance. Without
            // this, the picked value's own base-ui item can, in the same commit that
            // both changes `value` and unmounts that no-longer-relevant item, get its
            // selection silently reset back to null — reproduced directly against
            // base-ui's SelectPositioner `onMapChange`, whose registered-items
            // consistency check runs against a value ref that hasn't caught up yet. A
            // fresh instance has no "previous" state to reconcile against, so the
            // race can't occur. The key is unconditionally namespaced with a
            // role-specific prefix, not only in the empty-value fallback (Select Fix
            // Final Micro-Hardening §1) — a real-browser manual-acceptance defect
            // traced to a duplicate React key between two sibling Selects that both
            // fell back to the identical literal `"none"` (see
            // ProductSellingOptionPicker.tsx). This Select has no such sibling
            // today, but prefixing every value (not only the empty fallback) makes
            // uniqueness structural rather than incidental, matching the same
            // discipline applied there.
            key={`cq-product-${line?.product_id ?? "none"}`}
            value={line?.product_id || ""}
            onValueChange={(value) =>
              // Marks dirty (Final Hardening §1 BLOCKER) — a Custom Quantity Product
              // selection is exactly the kind of meaningful controlled edit the
              // unsaved-changes blocker exists to catch.
              setValue(`lines.${index}.product_id`, value ?? "", { shouldDirty: true })
            }
            disabled={operationalDisabled}
          >
            <SelectTrigger aria-label={`Line ${index + 1} product`}>
              {/* Render-prop, not a plain `placeholder` — a carried-forward inactive
                  Product (Final Hardening §2) is absent from `products` (active-only)
                  by construction, so without this the trigger would show a blank/raw
                  id instead of the Product's real name. */}
              <SelectValue>
                {(value: string) => {
                  if (!value) return "Select a product";
                  const activeMatch = products.find((p) => p.id === value);
                  if (activeMatch) return activeMatch.name;
                  if (
                    customQuantityProductDetailQuery.data &&
                    customQuantityProductDetailQuery.data.id === value
                  ) {
                    return customQuantityProductDetailQuery.data.is_active
                      ? customQuantityProductDetailQuery.data.name
                      : `${customQuantityProductDetailQuery.data.name} (archived)`;
                  }
                  return line?.display_name ?? "Loading…";
                }}
              </SelectValue>
            </SelectTrigger>
            <SelectContent>
              {products.map((product) => (
                <SelectItem key={product.id} value={product.id}>
                  {product.name}
                </SelectItem>
              ))}
              {/* The currently-attached Product, only when it's inactive (so it's
                  missing from `products` above) — visible/preserved as "leave
                  unchanged", never offered once the seller picks a different
                  (necessarily active) Product. Rendered LAST, after the regular
                  list: base-ui assigns each item an index by registration order, and
                  this item unmounting the instant the seller picks a different,
                  already-active Product (which is exactly when this condition flips
                  false) must never shift any earlier item's index out from under a
                  click that's still resolving — placing it last means removing it
                  can only ever affect indices after the one just picked. */}
              {line?.product_id &&
                !isCustomQuantityProductInActiveList &&
                customQuantityProductDetailQuery.data && (
                  <SelectItem value={line.product_id}>
                    {customQuantityProductDetailQuery.data.name} (archived — current)
                  </SelectItem>
                )}
            </SelectContent>
          </Select>
          <FormField id={`lines.${index}.underlying_quantity`} label="Quantity">
            <Input
              id={`lines.${index}.underlying_quantity`}
              inputMode="decimal"
              disabled={operationalDisabled}
              {...register(`lines.${index}.underlying_quantity`)}
            />
          </FormField>
          <FormField id={`lines.${index}.charged_unit_price`} label="Agreed line price">
            <CurrencyInput
              id={`lines.${index}.charged_unit_price`}
              {...register(`lines.${index}.charged_unit_price`)}
            />
          </FormField>
          {/* Manual Acceptance Pricing/UX Correction §5 — makes explicit that this is
              the whole-line customer charge (package_quantity is always 1 for this
              line type), not a per-unit rate multiplied by underlying_quantity. */}
          <p className="text-xs text-muted-foreground">
            Total customer charge for this custom quantity. Example: 30 cookies for
            $55 → line total $55.
          </p>
          <FormField
            id={`lines.${index}.packaging_cost_per_package`}
            label="Packaging cost override (internal, optional)"
          >
            <CurrencyInput
              id={`lines.${index}.packaging_cost_per_package`}
              placeholder={
                line?.currentPackagingSnapshot
                  ? `Currently $${line.currentPackagingSnapshot}`
                  : undefined
              }
              {...register(`lines.${index}.packaging_cost_per_package`)}
            />
          </FormField>
          {/* Wording depends on whether this is a persisted line (Final Hardening §3)
              — the stored value is a snapshot, not necessarily the Product's current
              default; only an actual Product change adopts the new default. */}
          {line?.serverId ? (
            <p className="text-xs text-muted-foreground">
              {line?.currentPackagingSnapshot
                ? `Stored packaging snapshot: $${line.currentPackagingSnapshot}. `
                : ""}
              Leave the override blank to preserve this value. If you change the
              product, leaving it blank uses the new product's current default
              packaging cost.
            </p>
          ) : (
            <p className="text-xs text-muted-foreground">
              Leave blank to use the selected product's current default packaging cost.
            </p>
          )}
          {/* Manual Acceptance Pricing/UX Correction §5 — packaging is an internal
              direct cost; it never adds to the customer-facing total above. */}
          <p className="text-xs text-muted-foreground">
            Internal packaging cost only. This does not change the customer-facing
            order total.
          </p>
          <FormField id={`lines.${index}.price_override_reason`} label="Pricing notes (optional)">
            <Input
              id={`lines.${index}.price_override_reason`}
              {...register(`lines.${index}.price_override_reason`)}
            />
          </FormField>
        </>
      )}

      {lineType === "CUSTOM_ITEM" && (
        <>
          <FormField id={`lines.${index}.display_name`} label="Description">
            <Input
              id={`lines.${index}.display_name`}
              {...register(`lines.${index}.display_name`)}
            />
          </FormField>
          <FormField id={`lines.${index}.underlying_quantity`} label="Quantity">
            <Input
              id={`lines.${index}.underlying_quantity`}
              inputMode="decimal"
              {...register(`lines.${index}.underlying_quantity`)}
            />
          </FormField>
          <FormField id={`lines.${index}.charged_unit_price`} label="Unit price">
            <CurrencyInput
              id={`lines.${index}.charged_unit_price`}
              {...register(`lines.${index}.charged_unit_price`)}
            />
          </FormField>
          {/* Manual Acceptance Pricing/UX Correction §6 — makes explicit that
              quantity × unit price is the entire customer-facing subtotal; none of
              the internal fields below change it. */}
          <p className="text-xs text-muted-foreground">
            Customer price per unit. Quantity × unit price = customer-facing line
            subtotal.
          </p>
          <FormField
            id={`lines.${index}.custom_direct_cost_estimate`}
            label="Internal direct-cost estimate (optional)"
          >
            <CurrencyInput
              id={`lines.${index}.custom_direct_cost_estimate`}
              {...register(`lines.${index}.custom_direct_cost_estimate`)}
            />
          </FormField>
          <FormField
            id={`lines.${index}.custom_active_time_minutes`}
            label="Estimated active time, minutes (internal, optional)"
          >
            <Input
              id={`lines.${index}.custom_active_time_minutes`}
              inputMode="numeric"
              {...register(`lines.${index}.custom_active_time_minutes`)}
            />
          </FormField>
          <p className="text-xs text-muted-foreground">
            Internal cost/workload information for your own planning — neither field
            changes the customer-facing price above.
          </p>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" {...register(`lines.${index}.manual_fulfillment_required`)} />
            Requires manual fulfillment tracking
          </label>
          <p className="text-xs text-muted-foreground">
            Internal operational tracking for your own reference; not yet tied to
            order readiness, and never changes the customer-facing price.
          </p>
        </>
      )}

      <FormField id={`lines.${index}.notes`} label="Line notes">
        <Textarea id={`lines.${index}.notes`} {...register(`lines.${index}.notes`)} />
      </FormField>
      {errorMessage && (
        <p className="text-xs text-destructive" role="alert">
          {errorMessage}
        </p>
      )}
    </div>
  );
}
