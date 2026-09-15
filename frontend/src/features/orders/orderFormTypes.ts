// Shared form schema/types/helpers between OrderEntryPage and OrderLineRow — split out
// so the row component (which needs the exact same `FormValues`/`LineFormValue` shape)
// doesn't have to import from the page component module.

import { z } from "zod";
import { formatDecimal } from "../../lib/decimal";
import type { Order, OrderLineType } from "./api";

function isValidDecimalString(value?: string): boolean {
  return value !== undefined && value.trim() !== "" && !Number.isNaN(Number(value));
}

function isPositiveDecimalString(value?: string): boolean {
  return isValidDecimalString(value) && Number(value) > 0;
}

// Line-type-aware validation (Checkpoint-3 correction 1) — a STANDARD_OPTION line has
// no `underlying_quantity` input at all (it's derived server-side from package_quantity
// x the Selling Option's multiplier), so requiring it unconditionally at the common
// schema level would make a legitimate Standard Option create flow fail client-side
// before the API is ever called. Each line type instead declares exactly what it needs.
export const lineSchema = z
  .object({
    serverId: z.string().optional(),
    line_type: z.enum(["STANDARD_OPTION", "CUSTOM_QUANTITY", "CUSTOM_ITEM"]),
    product_id: z.string().optional(),
    selling_option_id: z.string().optional(),
    package_quantity: z.string().optional(),
    underlying_quantity: z.string().optional(),
    charged_unit_price: z.string().optional(),
    price_override_reason: z.string().optional(),
    packaging_cost_per_package: z.string().optional(),
    display_name: z.string().optional(),
    custom_direct_cost_estimate: z.string().optional(),
    custom_active_time_minutes: z.string().optional(),
    manual_fulfillment_required: z.boolean().optional(),
    notes: z.string().optional(),
    // Display-only context — the currently-stored snapshot, shown as a read-only hint
    // next to the override input. Never submitted to the API (Checkpoint-3 corrections
    // 2/3): the actual override fields above start blank on edit so an unrelated or
    // source-change edit never silently resubmits the old snapshot as a newly declared
    // override.
    currentPriceSnapshot: z.string().optional(),
    currentPackagingSnapshot: z.string().optional(),
  })
  .superRefine((line, ctx) => {
    const issue = (path: string, message: string) =>
      ctx.addIssue({ code: z.ZodIssueCode.custom, message, path: [path] });

    if (line.line_type === "STANDARD_OPTION") {
      if (!line.product_id) issue("product_id", "Product is required");
      if (!line.selling_option_id) issue("selling_option_id", "Selling option is required");
      if (!isPositiveDecimalString(line.package_quantity)) {
        issue("package_quantity", "Package quantity must be greater than 0");
      }
      if (line.charged_unit_price && !isValidDecimalString(line.charged_unit_price)) {
        issue("charged_unit_price", "Enter a valid price");
      }
    } else if (line.line_type === "CUSTOM_QUANTITY") {
      if (!line.product_id) issue("product_id", "Product is required");
      if (!isPositiveDecimalString(line.underlying_quantity)) {
        issue("underlying_quantity", "Quantity must be greater than 0");
      }
      if (!isValidDecimalString(line.charged_unit_price)) {
        issue("charged_unit_price", "Agreed price is required");
      }
    } else {
      // CUSTOM_ITEM
      if (!line.display_name) issue("display_name", "Description is required");
      if (!isPositiveDecimalString(line.underlying_quantity)) {
        issue("underlying_quantity", "Quantity must be greater than 0");
      }
      if (!isValidDecimalString(line.charged_unit_price)) {
        issue("charged_unit_price", "Unit price is required");
      }
    }
  });

export const schema = z.object({
  customer_id: z.string().optional(),
  fulfillment_date: z.string().optional(),
  fulfillment_time: z.string().optional(),
  fulfillment_method: z.string().optional(),
  fulfillment_details: z.string().optional(),
  fulfillment_notes: z.string().optional(),
  internal_notes: z.string().optional(),
  order_adjustment: z.string().optional(),
  adjustment_description: z.string().optional(),
  manual_tax: z.string().optional(),
  lines: z.array(lineSchema),
  include_payment: z.boolean().optional(),
  payment_amount: z.string().optional(),
  payment_method: z.string().optional(),
  payment_date: z.string().optional(),
  payment_notes: z.string().optional(),
  confirm_overpayment: z.boolean().optional(),
});

export type FormValues = z.infer<typeof schema>;
export type LineFormValue = FormValues["lines"][number];

export function emptyLine(lineType: OrderLineType): LineFormValue {
  return {
    line_type: lineType,
    manual_fulfillment_required: lineType === "CUSTOM_ITEM" ? true : undefined,
  };
}

export function toFormValues(order?: Order): FormValues {
  return {
    customer_id: order?.customer_id ?? "",
    fulfillment_date: order?.fulfillment_date ?? "",
    fulfillment_time: order?.fulfillment_time ?? "",
    fulfillment_method: order?.fulfillment_method ?? "",
    fulfillment_details: order?.fulfillment_details ?? "",
    fulfillment_notes: order?.fulfillment_notes ?? "",
    internal_notes: order?.internal_notes ?? "",
    order_adjustment: order?.order_adjustment ?? "0",
    adjustment_description: order?.adjustment_description ?? "",
    manual_tax: order?.manual_tax ?? "0",
    lines: (order?.lines ?? []).map((line) => ({
      serverId: line.id,
      line_type: line.line_type,
      product_id: line.product_id ?? undefined,
      selling_option_id: line.selling_option_id ?? undefined,
      // Manual Acceptance Pricing/UX Correction §4 — the API's persisted
      // NUMERIC(18,6) strings ("3.000000") are numerically correct but read as
      // unnecessarily database-like in an editable field; `formatDecimal` (already
      // used throughout the app's read-only Decimal displays) strips only
      // insignificant trailing zeros via pure string manipulation, never routing
      // the value through JS `Number`, so no precision is at risk and the value
      // submitted on save is unaffected (it's re-derived from whatever the seller
      // actually typed, not from this display string).
      package_quantity: line.package_quantity ? formatDecimal(line.package_quantity) : line.package_quantity,
      underlying_quantity: line.underlying_quantity
        ? formatDecimal(line.underlying_quantity)
        : line.underlying_quantity,
      // STANDARD_OPTION: charged_unit_price is an explicit price OVERRIDE field, not
      // simply "the currently stored snapshot" — starting it blank means an unrelated
      // or source-change edit never silently resubmits the old snapshot as a newly
      // declared override (Checkpoint-3 correction 2). CUSTOM_QUANTITY/CUSTOM_ITEM have
      // no such distinction: their price is always the literal current agreed price, so
      // it still populates from the stored snapshot.
      charged_unit_price:
        line.line_type === "STANDARD_OPTION" ? "" : line.charged_unit_price_snapshot,
      currentPriceSnapshot: line.charged_unit_price_snapshot,
      price_override_reason: line.price_override_reason ?? "",
      // CUSTOM_QUANTITY: packaging_cost_per_package is likewise an explicit override
      // field (Checkpoint-3 correction 3) — starts blank so a Product source change can
      // adopt the new Product's own default rather than resubmitting the old value as
      // an override that pins the old packaging cost forever.
      packaging_cost_per_package:
        line.line_type === "CUSTOM_QUANTITY" ? "" : line.packaging_cost_per_package_snapshot,
      currentPackagingSnapshot: line.packaging_cost_per_package_snapshot,
      display_name: line.display_name_snapshot,
      custom_direct_cost_estimate: line.custom_direct_cost_estimate ?? "",
      custom_active_time_minutes: line.custom_active_time_minutes?.toString() ?? "",
      manual_fulfillment_required: line.manual_fulfillment_required,
      notes: line.notes ?? "",
    })),
    include_payment: false,
    payment_amount: "",
    payment_method: "",
    payment_date: "",
    payment_notes: "",
    confirm_overpayment: false,
  };
}
