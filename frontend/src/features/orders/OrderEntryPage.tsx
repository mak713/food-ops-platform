// Single-page Order Entry (Final Plan §I / Spec §12.7): Customer -> Fulfillment ->
// Order Items -> Notes -> Optional Payment -> Order Summary -> Operational Impact
// Preview. One component serves both create and edit (isEdit = Boolean(id)), matching
// CustomerFormPage.tsx's convention exactly.
//
// Stable server identity: each field-array row's persisted `serverId` is a distinct form
// property from React Hook Form's own internal per-row key (renamed via `keyName:
// "rhfKey"` on useFieldArray) — the two concepts never share a property name, so RHF's
// internal render key can never be accidentally submitted as an OrderLine UUID (Final
// Pre-Implementation Amendment §8).
//
// No Confirm button anywhere (Final Plan §C) — this page only creates/edits a Draft.

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useFieldArray, useForm, useWatch } from "react-hook-form";
import { useBlocker, useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { CurrencyInput } from "../../components/shared/CurrencyInput";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { FormField } from "../../components/shared/FormField";
import { StaleVersionPanel } from "../../components/shared/StaleVersionPanel";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../../components/ui/alert-dialog";
import { Button } from "../../components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../components/ui/dialog";
import { Input } from "../../components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import { Textarea } from "../../components/ui/textarea";
import { customersApi } from "../customers/api";
import { productsApi, type Product } from "../products/api";
import { ordersApi, type OrderLine, type OrderLineInput } from "./api";
import { OrderLineRow } from "./OrderLineRow";
import { emptyLine, schema, toFormValues, type FormValues, type LineFormValue } from "./orderFormTypes";

const GUEST_SENTINEL = "__guest__";

// Manual Acceptance Pricing/UX Correction §2 (BLOCKER) / §9 — the non-authoritative
// customer-facing total preview must use the same approved effective-price intent
// semantics the backend itself applies, not silently treat a blank Standard Option price
// override as zero. Finds the line's ORIGINAL stored source (product/selling option), if
// any, so the preview can distinguish "retained, same source" (preserve the stored
// snapshot) from "retained, actual source change" (use the new source's live catalog
// price) — mirroring the backend's own `source_changed = product_changed OR
// option_changed` rule (order_service.py) exactly, on the client's already-loaded data,
// never a fabricated guess.
function findOriginalLine(
  existingOrder: { lines: OrderLine[] } | undefined,
  serverId: string | undefined,
): OrderLine | undefined {
  if (!existingOrder || !serverId) return undefined;
  return existingOrder.lines.find((l) => l.id === serverId);
}

function effectiveStandardOptionUnitPrice(
  line: LineFormValue,
  originalLine: OrderLine | undefined,
  productDetailByProductId: Map<string, Product>,
): number {
  // Explicit override always wins, for a new line or a retained one alike.
  if (line.charged_unit_price && !Number.isNaN(Number(line.charged_unit_price))) {
    return Number(line.charged_unit_price);
  }
  const liveCatalogPrice = (): number => {
    const product = line.product_id ? productDetailByProductId.get(line.product_id) : undefined;
    const option = product?.selling_options.find((o) => o.id === line.selling_option_id);
    return option ? Number(option.price) : 0;
  };
  if (!line.serverId || !originalLine) {
    // A brand-new line has no stored snapshot to preserve — only the live catalog
    // price is ever meaningful here.
    return liveCatalogPrice();
  }
  const sourceChanged =
    (line.product_id ?? "") !== (originalLine.product_id ?? "") ||
    (line.selling_option_id ?? "") !== (originalLine.selling_option_id ?? "");
  if (sourceChanged) {
    return liveCatalogPrice();
  }
  // Retained, same source: preserve the stored snapshot — never silently refresh to
  // whatever the catalog price happens to be right now.
  return Number(line.currentPriceSnapshot || originalLine.charged_unit_price_snapshot || 0);
}

function toLineInput(line: LineFormValue): OrderLineInput {
  const isStandardOption = line.line_type === "STANDARD_OPTION";
  const isCustomQuantity = line.line_type === "CUSTOM_QUANTITY";
  const isCustomItem = line.line_type === "CUSTOM_ITEM";
  return {
    id: line.serverId ?? null,
    line_type: line.line_type,
    product_id: !isCustomItem ? line.product_id || null : null,
    selling_option_id: isStandardOption ? line.selling_option_id || null : null,
    package_quantity: isStandardOption ? line.package_quantity || null : null,
    underlying_quantity: line.underlying_quantity || null,
    // Blank -> null for STANDARD_OPTION means "no override, use the current catalog
    // price" (Checkpoint-3 correction 2); for CUSTOM_QUANTITY/CUSTOM_ITEM it's always
    // the literal required price.
    charged_unit_price: line.charged_unit_price || null,
    // CUSTOM_ITEM has no price-override concept at all — the backend rejects a
    // non-null value here for that type (Checkpoint-3 correction 12).
    price_override_reason: !isCustomItem ? line.price_override_reason || null : null,
    // Blank -> null for CUSTOM_QUANTITY means "preserve the existing snapshot, or the
    // new product's default on a source change" (Checkpoint-3 correction 3).
    packaging_cost_per_package: isCustomQuantity ? line.packaging_cost_per_package || null : null,
    display_name: isCustomItem ? line.display_name || null : null,
    custom_direct_cost_estimate: isCustomItem ? line.custom_direct_cost_estimate || null : null,
    custom_active_time_minutes:
      isCustomItem && line.custom_active_time_minutes
        ? Number(line.custom_active_time_minutes)
        : null,
    manual_fulfillment_required: isCustomItem ? (line.manual_fulfillment_required ?? null) : null,
    notes: line.notes || null,
  };
}

function clientPreviewSubtotal(
  line: LineFormValue,
  originalLine: OrderLine | undefined,
  productDetailByProductId: Map<string, Product>,
): number {
  if (line.line_type === "STANDARD_OPTION") {
    // package_quantity × effective package price (Manual Acceptance Pricing/UX
    // Correction §2/§9) — "package_quantity" genuinely means the number of packages
    // of the selected Selling Option, not an individual-unit count; e.g. 30 packages
    // × $3/package = $90 is correct even though "30 cookies × $3" would mean
    // something different (see the Package quantity field's own clarified help text).
    const qty = Number(line.package_quantity || 0);
    const price = effectiveStandardOptionUnitPrice(line, originalLine, productDetailByProductId);
    if (Number.isNaN(qty) || Number.isNaN(price)) return 0;
    return qty * price;
  }
  if (line.line_type === "CUSTOM_QUANTITY") {
    // The seller-agreed whole-line price is the entire customer-facing contribution,
    // regardless of underlying_quantity or any packaging-cost override (Manual
    // Acceptance Pricing/UX Correction §5/§9) — package_quantity is always 1 for this
    // line type, so this was already numerically correct; stated explicitly here so
    // it's not confused with the STANDARD_OPTION qty×price shape above.
    const price = Number(line.charged_unit_price || 0);
    return Number.isNaN(price) ? 0 : price;
  }
  // CUSTOM_ITEM: quantity × unit price (Manual Acceptance Pricing/UX Correction §6/§9)
  // — internal fields (direct-cost estimate, active-time estimate, manual-fulfillment
  // toggle) never factor into this customer-facing figure.
  const qty = Number(line.underlying_quantity || 0);
  const price = Number(line.charged_unit_price || 0);
  if (Number.isNaN(qty) || Number.isNaN(price)) return 0;
  return qty * price;
}

export function OrderEntryPage() {
  const { id } = useParams<{ id: string }>();
  const isEdit = Boolean(id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [newCustomerOpen, setNewCustomerOpen] = useState(false);
  const [newCustomerName, setNewCustomerName] = useState("");
  const [duplicateWarning, setDuplicateWarning] = useState<string | null>(null);
  // Suppresses the unsaved-changes navigation blocker for the programmatic navigate()
  // that follows a successful save — the form is "dirty" relative to its original
  // load, but there is nothing left to lose (Checkpoint-3 correction 5). A ref, not
  // state: `navigate()` runs synchronously right after this is set, and the blocker's
  // predicate must see the updated value immediately — a `useState` setter's update
  // wouldn't be visible to that closure until the next render, letting the blocker
  // fire on stale `false` and incorrectly trap the post-save navigation.
  const justSavedRef = useRef(false);

  const existingQuery = useQuery({
    queryKey: ["orders", id],
    queryFn: () => ordersApi.get(id as string),
    enabled: isEdit,
    retry: false,
  });

  const customersQuery = useQuery({
    queryKey: ["customers", { is_active: true, limit: 200 }],
    queryFn: () => customersApi.list({ is_active: true, limit: 200 }),
  });

  const productsQuery = useQuery({
    queryKey: ["products", { is_active: true, limit: 200 }],
    queryFn: () => productsApi.list({ is_active: true, limit: 200 }),
  });

  const {
    register,
    control,
    handleSubmit,
    setValue,
    reset,
    formState: { errors, isDirty },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: toFormValues(),
  });

  const { fields, append, remove } = useFieldArray({
    control,
    name: "lines",
    keyName: "rhfKey",
  });

  // `useWatch` (a proper hook, called unconditionally here) rather than calling the
  // `watch("lines")` function procedurally inside the render body below — the latter is
  // exactly the pattern that trips the React Compiler's "incompatible library" warning
  // (Checkpoint-3 correction 17); `OrderLineRow` uses the identical `useWatch` approach
  // for its own per-row value, scoped to just that row.
  const lineValues = useWatch({ control, name: "lines" });
  const customerId = useWatch({ control, name: "customer_id" });
  const fulfillmentMethod = useWatch({ control, name: "fulfillment_method" });
  const includePayment = useWatch({ control, name: "include_payment" });
  const orderAdjustment = useWatch({ control, name: "order_adjustment" });
  const manualTax = useWatch({ control, name: "manual_tax" });

  // Carried-forward reference display (Final Hardening §2 / ADR-108) — a Draft's
  // customer_id can point at a Customer that has since become inactive; the active-only
  // list above would silently exclude it, and without this the trigger has nothing to
  // resolve the id against and falls back to "Guest / Walk-In" even though a real
  // Customer is still attached. Fetched unconditionally whenever a customer is selected
  // (mirrors ProductSellingOptionPicker's existing unconditional per-id detail fetch) so
  // its `is_active`/`name` are always authoritative, not inferred from list membership.
  const selectedCustomerQuery = useQuery({
    queryKey: ["customers", customerId],
    queryFn: () => customersApi.get(customerId as string),
    enabled: Boolean(customerId),
  });

  // Live Selling Option catalog prices for the customer-facing total preview's
  // STANDARD_OPTION lines (Manual Acceptance Pricing/UX Correction §2/§9). Each query
  // uses the exact same `["products", productId]` key `ProductSellingOptionPicker`
  // and `OrderLineRow`'s own Custom Quantity picker already fetch under — TanStack
  // Query's cache is shared by key, so this reuses whatever's already loaded for a
  // line the seller has open rather than issuing a duplicate request, and never
  // fetches the full Product list to derive this (no N+1 across the catalog).
  const standardOptionProductIds = Array.from(
    new Set(
      (lineValues ?? [])
        .filter((l) => l.line_type === "STANDARD_OPTION" && l.product_id)
        .map((l) => l.product_id as string),
    ),
  );
  const standardOptionProductDetailQueries = useQueries({
    queries: standardOptionProductIds.map((productId) => ({
      queryKey: ["products", productId],
      queryFn: () => productsApi.get(productId),
    })),
  });
  const productDetailByProductId = new Map<string, Product>(
    standardOptionProductDetailQueries
      .filter((q) => q.data)
      .map((q) => [q.data!.id, q.data!] as const),
  );

  useEffect(() => {
    if (existingQuery.data) {
      reset(toFormValues(existingQuery.data));
    }
  }, [existingQuery.data, reset]);

  // Unsaved-change protection (Spec §12.10; Checkpoint-3 correction 5) — no dependency
  // added: `useBlocker` is built into the installed react-router-dom v7 and requires a
  // data router (createBrowserRouter/createMemoryRouter), which this app already uses.
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      isDirty && !justSavedRef.current && currentLocation.pathname !== nextLocation.pathname,
  );

  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (isDirty && !justSavedRef.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [isDirty]);

  const createCustomerMutation = useMutation({
    mutationFn: (confirmDuplicate: boolean) =>
      customersApi.create({ name: newCustomerName, confirm_duplicate: confirmDuplicate }),
    onSuccess: async (customer) => {
      await queryClient.invalidateQueries({ queryKey: ["customers"] });
      setValue("customer_id", customer.id, { shouldDirty: true });
      setNewCustomerOpen(false);
      setNewCustomerName("");
      setDuplicateWarning(null);
    },
    onError: (error) => {
      if (error instanceof ApiError) {
        const duplicate = error.body?.error.issues.find(
          (i) => i.code === "POSSIBLE_DUPLICATE_CUSTOMER",
        );
        if (duplicate) {
          setDuplicateWarning(duplicate.message);
          return;
        }
      }
      setDuplicateWarning("Something went wrong creating the customer.");
    },
  });

  const mutation = useMutation({
    mutationFn: (values: FormValues) => {
      const lines = values.lines.map(toLineInput);
      if (isEdit) {
        if (!existingQuery.data) {
          throw new Error("Cannot save: the original record failed to load.");
        }
        return ordersApi.update(existingQuery.data.id, {
          version: existingQuery.data.version,
          customer_id: values.customer_id || null,
          fulfillment_date: values.fulfillment_date || null,
          fulfillment_time: values.fulfillment_time || null,
          fulfillment_method: (values.fulfillment_method as never) || null,
          fulfillment_details: values.fulfillment_details || null,
          fulfillment_notes: values.fulfillment_notes || null,
          internal_notes: values.internal_notes || null,
          order_adjustment: values.order_adjustment || "0",
          adjustment_description: values.adjustment_description || null,
          manual_tax: values.manual_tax || "0",
          lines,
          confirm_overpayment: values.confirm_overpayment ?? false,
        });
      }
      return ordersApi.create({
        customer_id: values.customer_id || null,
        fulfillment_date: values.fulfillment_date || null,
        fulfillment_time: values.fulfillment_time || null,
        fulfillment_method: (values.fulfillment_method as never) || null,
        fulfillment_details: values.fulfillment_details || null,
        fulfillment_notes: values.fulfillment_notes || null,
        internal_notes: values.internal_notes || null,
        order_adjustment: values.order_adjustment || "0",
        adjustment_description: values.adjustment_description || null,
        manual_tax: values.manual_tax || "0",
        lines,
        payment: values.include_payment
          ? {
              amount: values.payment_amount || "0",
              payment_method: values.payment_method || "",
              payment_date: values.payment_date || "",
              notes: values.payment_notes || null,
              confirm_overpayment: values.confirm_overpayment ?? false,
            }
          : null,
      });
    },
    onSuccess: async (order) => {
      await queryClient.invalidateQueries({ queryKey: ["orders"] });
      justSavedRef.current = true;
      navigate(`/app/orders/${order.id}`);
    },
  });

  const submit = handleSubmit((values) => mutation.mutate(values));

  const isStaleVersion =
    mutation.error instanceof ApiError && mutation.error.body?.error.code === "STALE_VERSION";
  const overpaymentIssue =
    mutation.error instanceof ApiError
      ? mutation.error.body?.error.issues.find((i) => i.code === "PAYMENT_WOULD_OVERPAY")
      : undefined;

  const handleRefreshAfterStaleVersion = async () => {
    mutation.reset();
    const fresh = await existingQuery.refetch();
    if (fresh.data) {
      reset(toFormValues(fresh.data));
    }
  };

  if (isEdit && existingQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (isEdit && existingQuery.isError) {
    const isNotFound =
      existingQuery.error instanceof ApiError && existingQuery.error.status === 404;
    if (isNotFound) {
      return (
        <div className="flex flex-col gap-2">
          <h1 className="text-xl font-semibold">Order not found</h1>
          <Button variant="outline" onClick={() => navigate("/app/orders")}>
            Back to orders
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={existingQuery.error} onRetry={() => existingQuery.refetch()} />;
  }

  const subtotalPreview = (lineValues ?? []).reduce(
    (sum, l) =>
      sum +
      clientPreviewSubtotal(
        l,
        findOriginalLine(existingQuery.data, l.serverId),
        productDetailByProductId,
      ),
    0,
  );
  const adjustmentPreview = Number(orderAdjustment || 0);
  const taxPreview = Number(manualTax || 0);
  const finalTotalPreview = subtotalPreview + adjustmentPreview + taxPreview;
  const products = productsQuery.data?.items ?? [];

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_320px]">
      <form className="flex flex-col gap-8" onSubmit={submit}>
        <h1 className="text-xl font-semibold">{isEdit ? "Edit Order" : "New Order"}</h1>

        {/* --- Customer --- */}
        <section className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">Customer</h2>
          <div className="flex items-center gap-2">
            <Select
              // Keyed by the current value: switching away from an inactive Customer
              // removes the archived-current item (below) from this list in the same
              // commit that also changes `value` — under an already-mounted base-ui
              // Select instance, that combination hits a real base-ui bug where the
              // just-made selection gets silently reset back to null (reproduced
              // directly against SelectPositioner `onMapChange`). A remounted
              // instance has no stale "previous" state to reconcile against.
              key={customerId || GUEST_SENTINEL}
              value={customerId || GUEST_SENTINEL}
              onValueChange={(value) =>
                setValue("customer_id", value === GUEST_SENTINEL ? "" : (value ?? ""), {
                  shouldDirty: true,
                })
              }
            >
              <SelectTrigger aria-label="Customer" className="min-w-48">
                {/* Explicit render-prop rather than relying on SelectContent's item
                    registry (which only populates once the popup has mounted) to
                    resolve the label — otherwise the trigger can briefly show the raw
                    sentinel value instead of "Guest / Walk-In" before first open. Also
                    the only reliable way to label a carried-forward inactive Customer
                    (Final Hardening §2): it's absent from the active list by
                    construction, so falling back to "Guest / Walk-In" for a non-empty,
                    non-sentinel value would misrepresent an Order that genuinely still
                    has a Customer attached. */}
                <SelectValue>
                  {(value: string) => {
                    if (!value || value === GUEST_SENTINEL) return "Guest / Walk-In";
                    const activeMatch = (customersQuery.data?.items ?? []).find(
                      (c) => c.id === value,
                    );
                    if (activeMatch) return activeMatch.name;
                    if (selectedCustomerQuery.data && selectedCustomerQuery.data.id === value) {
                      return selectedCustomerQuery.data.is_active
                        ? selectedCustomerQuery.data.name
                        : `${selectedCustomerQuery.data.name} (archived)`;
                    }
                    return "Loading…";
                  }}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={GUEST_SENTINEL}>Guest / Walk-In</SelectItem>
                {(customersQuery.data?.items ?? []).map((customer) => (
                  <SelectItem key={customer.id} value={customer.id}>
                    {customer.name}
                  </SelectItem>
                ))}
                {/* The currently-attached Customer, only when it's inactive (so it's
                    missing from the active list above) — keeps it selectable/visible
                    as "leave unchanged" without offering it as a choice once the
                    seller switches away, since it then stops being the form's value
                    and this branch stops rendering (Final Hardening §2). Rendered
                    LAST, after Guest and every active Customer: base-ui assigns each
                    item an index by registration order, and this item unmounting the
                    instant the seller switches to a *different* Customer/Guest (which
                    is exactly when this condition flips false) must never shift an
                    earlier item's index out from under a click that's still
                    resolving — placing it last means removing it can only ever
                    affect indices after the one just picked (reproduced directly
                    against base-ui's SelectPositioner `onMapChange`, which otherwise
                    resets the just-made selection back to null). */}
                {customerId &&
                  selectedCustomerQuery.data &&
                  !selectedCustomerQuery.data.is_active &&
                  !(customersQuery.data?.items ?? []).some((c) => c.id === customerId) && (
                    <SelectItem value={customerId}>
                      {selectedCustomerQuery.data.name} (archived — current)
                    </SelectItem>
                  )}
              </SelectContent>
            </Select>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setNewCustomerOpen(true)}
            >
              New customer
            </Button>
          </div>
        </section>

        {/* --- Fulfillment --- */}
        <section className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">Fulfillment</h2>
          <div className="grid grid-cols-2 gap-3">
            <FormField id="fulfillment_date" label="Fulfillment date">
              <Input id="fulfillment_date" type="date" {...register("fulfillment_date")} />
            </FormField>
            <FormField id="fulfillment_time" label="Fulfillment time">
              <Input id="fulfillment_time" type="time" {...register("fulfillment_time")} />
            </FormField>
          </div>
          <Select
            value={fulfillmentMethod || ""}
            onValueChange={(value) => setValue("fulfillment_method", value ?? "", { shouldDirty: true })}
          >
            <SelectTrigger aria-label="Fulfillment method">
              <SelectValue placeholder="Fulfillment method" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="PICKUP">Pickup</SelectItem>
              <SelectItem value="DELIVERY">Delivery</SelectItem>
              <SelectItem value="OTHER">Other</SelectItem>
            </SelectContent>
          </Select>
          <FormField id="fulfillment_details" label="Fulfillment details">
            <Textarea id="fulfillment_details" {...register("fulfillment_details")} />
          </FormField>
          <FormField id="fulfillment_notes" label="Fulfillment-facing notes">
            <Textarea id="fulfillment_notes" {...register("fulfillment_notes")} />
          </FormField>
        </section>

        {/* --- Order Items --- */}
        <section className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">Order Items</h2>
          {fields.map((field, index) => {
            const lineErrors = errors.lines?.[index];
            const errorMessage = lineErrors
              ? Object.values(lineErrors)
                  .map((e) => (e && typeof e === "object" && "message" in e ? e.message : undefined))
                  .find((m): m is string => Boolean(m))
              : undefined;
            return (
              <OrderLineRow
                key={field.rhfKey}
                control={control}
                index={index}
                register={register}
                setValue={setValue}
                remove={remove}
                products={products}
                errorMessage={errorMessage}
              />
            );
          })}
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => append(emptyLine("STANDARD_OPTION"))}
            >
              Add Standard Option line
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => append(emptyLine("CUSTOM_QUANTITY"))}
            >
              Add Custom Quantity line
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => append(emptyLine("CUSTOM_ITEM"))}
            >
              Add Custom Item line
            </Button>
          </div>
        </section>

        {/* --- Notes --- */}
        <section className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">Notes</h2>
          <FormField id="internal_notes" label="Internal notes (never shown to the customer)">
            <Textarea
              id="internal_notes"
              className="border-amber-500/50"
              {...register("internal_notes")}
            />
          </FormField>
        </section>

        {/* --- Optional Payment (create only) --- */}
        {!isEdit && (
          <section className="flex flex-col gap-3">
            <h2 className="text-lg font-semibold">Optional Payment</h2>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" {...register("include_payment")} />
              Record a payment now
            </label>
            {includePayment && (
              <div className="flex flex-col gap-2">
                <FormField id="payment_amount" label="Amount">
                  <CurrencyInput id="payment_amount" {...register("payment_amount")} />
                </FormField>
                <FormField id="payment_method" label="Payment method">
                  <Input id="payment_method" {...register("payment_method")} />
                </FormField>
                <FormField id="payment_date" label="Payment date">
                  <Input id="payment_date" type="date" {...register("payment_date")} />
                </FormField>
                <FormField id="payment_notes" label="Payment notes (optional)">
                  <Input id="payment_notes" {...register("payment_notes")} />
                </FormField>
              </div>
            )}
          </section>
        )}

        {overpaymentIssue && (
          <div className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
            <p role="alert">{overpaymentIssue.message}</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setValue("confirm_overpayment", true);
                submit();
              }}
            >
              Confirm anyway
            </Button>
          </div>
        )}

        {isStaleVersion && <StaleVersionPanel onRefresh={handleRefreshAfterStaleVersion} />}

        {mutation.isError && !isStaleVersion && !overpaymentIssue && (
          <p role="alert" className="text-sm text-destructive">
            {mutation.error instanceof ApiError
              ? mutation.error.body?.error.message
              : "Something went wrong. Please try again."}
          </p>
        )}

        <Button type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? "Saving…" : "Save"}
        </Button>
      </form>

      {/* --- Order Summary + Operational Impact Preview (sticky on desktop) --- */}
      <aside className="flex flex-col gap-4 lg:sticky lg:top-4 lg:self-start">
        <div className="rounded-md border p-4">
          <h2 className="mb-2 text-sm font-semibold">Customer-facing total</h2>
          <dl className="flex flex-col gap-1 text-sm">
            <div className="flex justify-between">
              <dt>Subtotal</dt>
              <dd>${subtotalPreview.toFixed(2)}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Adjustment</dt>
              <dd>${adjustmentPreview.toFixed(2)}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Tax</dt>
              <dd>${taxPreview.toFixed(2)}</dd>
            </div>
            <div className="flex justify-between font-medium">
              <dt>Total</dt>
              <dd>${finalTotalPreview.toFixed(2)}</dd>
            </div>
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">
            Preview only — the server-computed total is authoritative once saved.
          </p>
          <FormField id="order_adjustment" label="Adjustment (+/-)">
            <CurrencyInput id="order_adjustment" {...register("order_adjustment")} />
          </FormField>
          <FormField id="adjustment_description" label="Adjustment description">
            <Input id="adjustment_description" {...register("adjustment_description")} />
          </FormField>
          <FormField id="manual_tax" label="Manual tax">
            <CurrencyInput id="manual_tax" {...register("manual_tax")} />
          </FormField>
        </div>

        <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
          <h2 className="mb-1 font-semibold">Internal estimate</h2>
          <p>Not yet calculated — available in a future phase.</p>
        </div>

        <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
          <h2 className="mb-1 font-semibold">Operational Impact Preview</h2>
          <p>Not yet available — coming in a future phase.</p>
        </div>
      </aside>

      <Dialog open={newCustomerOpen} onOpenChange={setNewCustomerOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New customer</DialogTitle>
          </DialogHeader>
          <FormField id="new_customer_name" label="Name">
            <Input
              id="new_customer_name"
              value={newCustomerName}
              onChange={(e) => setNewCustomerName(e.target.value)}
            />
          </FormField>
          {duplicateWarning && (
            <p role="alert" className="text-sm text-amber-600">
              {duplicateWarning}
            </p>
          )}
          <DialogFooter>
            {duplicateWarning ? (
              <Button
                type="button"
                variant="outline"
                onClick={() => createCustomerMutation.mutate(true)}
              >
                Create anyway
              </Button>
            ) : (
              <Button
                type="button"
                onClick={() => createCustomerMutation.mutate(false)}
                disabled={!newCustomerName || createCustomerMutation.isPending}
              >
                Create
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={blocker.state === "blocked"}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
            <AlertDialogDescription>
              This order has unsaved changes. Leaving now will discard them.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => blocker.state === "blocked" && blocker.reset()}>
              Stay on this page
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => blocker.state === "blocked" && blocker.proceed()}
            >
              Discard changes
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
