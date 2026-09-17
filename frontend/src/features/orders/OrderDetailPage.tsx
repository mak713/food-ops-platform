// Order Details (Final Plan §I): identity/status, fulfillment info, line snapshots,
// customer-facing totals, Payment history + inline add-form, derived payment
// status/overpayment, Draft-only actions (Edit/Delete-with-warning).
//
// Phase 7: real Confirm (DRAFT -> CONFIRMED) and Cancel (CONFIRMED -> CANCELED)
// actions, replacing the old "structural readiness only" placeholder. A rejected
// Confirm carrying operational warnings (`OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT`)
// opens one consolidated warning-review dialog — never a sequential modal chain —
// listing every warning at once; resubmitting echoes back each warning's own
// server-issued `details.fingerprint` unmodified (fingerprint construction stays
// entirely backend-owned, Plan v2 §10). `production_locked` is read-only/advisory
// and gates the Cancel action, and the Edit form's operational fields, in the UI
// only — the backend independently and authoritatively re-checks under its own
// lock regardless of this flag (Final Architecture Lock §C). Edit routes a
// CONFIRMED order to the same OrderEntryPage as a Draft edit — that page decides,
// from the order's own status, which Phase 7 service workflow the save actually
// uses (`update_confirmed_order` vs the Phase 6 `update_draft_order`).

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, useNavigate, useParams } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { CurrencyInput } from "../../components/shared/CurrencyInput";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { FormField } from "../../components/shared/FormField";
import { StaleVersionPanel } from "../../components/shared/StaleVersionPanel";
import { formatQuantityForDisplay } from "../../lib/decimal";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "../../components/ui/alert-dialog";
import { Badge } from "../../components/ui/badge";
import { Button, buttonVariants } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table";
import { ordersApi } from "./api";

const paymentSchema = z.object({
  amount: z.string().min(1, "Amount is required"),
  payment_method: z.string().min(1, "Payment method is required"),
  payment_date: z.string().min(1, "Payment date is required"),
  notes: z.string().optional(),
});
type PaymentFormValues = z.infer<typeof paymentSchema>;

const PAYMENT_STATUS_LABEL: Record<string, string> = {
  UNPAID: "Unpaid",
  PARTIALLY_PAID: "Partially Paid",
  PAID: "Paid",
};

export function OrderDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ["orders", id],
    queryFn: () => ordersApi.get(id as string),
    retry: false,
  });

  const invalidate = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["orders", id] }),
      queryClient.invalidateQueries({ queryKey: ["orders"] }),
    ]);

  const deleteMutation = useMutation({
    mutationFn: (confirmDeleteWithPayments: boolean) =>
      ordersApi.remove(id as string, query.data!.version, confirmDeleteWithPayments),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["orders"] });
      navigate("/app/orders");
    },
  });

  const confirmMutation = useMutation({
    mutationFn: (fingerprints: string[]) =>
      ordersApi.confirm(id as string, {
        version: query.data!.version,
        acknowledged_warning_fingerprints: fingerprints,
      }),
    onSuccess: invalidate,
  });
  const cancelMutation = useMutation({
    mutationFn: () => ordersApi.cancel(id as string, { version: query.data!.version }),
    onSuccess: invalidate,
  });

  const confirmWarningIssues =
    confirmMutation.error instanceof ApiError &&
    confirmMutation.error.body?.error.code === "OPERATIONAL_WARNINGS_REQUIRE_ACKNOWLEDGEMENT"
      ? confirmMutation.error.body.error.issues
      : undefined;
  const confirmGenericError =
    confirmMutation.isError && !confirmWarningIssues
      ? confirmMutation.error instanceof ApiError
        ? confirmMutation.error.body?.error.message
        : "Something went wrong confirming the order. Please try again."
      : undefined;
  const cancelGenericError =
    cancelMutation.isError && cancelMutation.error instanceof ApiError
      ? cancelMutation.error.body?.error.message
      : undefined;

  const handleConfirmAnyway = () => {
    const fingerprints = (confirmWarningIssues ?? [])
      .map((issue) => issue.details.fingerprint)
      .filter((f): f is string => typeof f === "string");
    confirmMutation.mutate(fingerprints);
  };

  const {
    register,
    handleSubmit,
    reset: resetPaymentForm,
    formState: { errors: paymentErrors },
  } = useForm<PaymentFormValues>({
    resolver: zodResolver(paymentSchema),
    defaultValues: { amount: "", payment_method: "", payment_date: "", notes: "" },
  });

  const addPaymentMutation = useMutation({
    mutationFn: (values: PaymentFormValues & { confirm_overpayment?: boolean }) =>
      ordersApi.addPayment(id as string, {
        amount: values.amount,
        payment_method: values.payment_method,
        payment_date: values.payment_date,
        notes: values.notes || null,
        confirm_overpayment: values.confirm_overpayment ?? false,
      }),
    onSuccess: async () => {
      await invalidate();
      resetPaymentForm();
    },
  });

  const submitPayment = handleSubmit((values) => addPaymentMutation.mutate(values));

  const overpaymentIssue =
    addPaymentMutation.error instanceof ApiError
      ? addPaymentMutation.error.body?.error.issues.find((i) => i.code === "PAYMENT_WOULD_OVERPAY")
      : undefined;

  const paymentsWarning =
    deleteMutation.error instanceof ApiError &&
    deleteMutation.error.body?.error.code === "ORDER_DELETE_HAS_PAYMENTS_WARNING"
      ? deleteMutation.error.body?.error.issues[0]
      : undefined;

  const isStaleVersion = (err: unknown) =>
    err instanceof ApiError && err.body?.error.code === "STALE_VERSION";
  const staleVersionOccurred = isStaleVersion(deleteMutation.error);

  const handleRefreshAfterStaleVersion = async () => {
    deleteMutation.reset();
    await query.refetch();
  };

  if (query.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (query.isError) {
    const isNotFound = query.error instanceof ApiError && query.error.status === 404;
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
    return <ErrorBanner error={query.error} onRetry={() => query.refetch()} />;
  }

  const order = query.data;
  if (!order) return null;
  const isDraft = order.status === "DRAFT";
  const isCanceled = order.status === "CANCELED";

  // Generic mutation-failure surfacing (Final Hardening §6) — the specific
  // overpayment-warning/has-payments-warning/stale-version cases already render their
  // own dedicated UI below; everything else (a lifecycle 409, a server error, a
  // validation error the local schema doesn't already catch) previously had no visible
  // feedback at all once the mutation settled into an error state.
  const addPaymentGenericError =
    addPaymentMutation.isError && !overpaymentIssue
      ? addPaymentMutation.error instanceof ApiError
        ? addPaymentMutation.error.body?.error.message
        : "Something went wrong adding the payment. Please try again."
      : undefined;
  const deleteGenericError =
    deleteMutation.isError && !paymentsWarning && !staleVersionOccurred
      ? deleteMutation.error instanceof ApiError
        ? deleteMutation.error.body?.error.message
        : "Something went wrong deleting the order. Please try again."
      : undefined;

  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_320px]">
      <div className="flex flex-col gap-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold">{order.order_number}</h1>
            <p className="text-sm text-muted-foreground">
              {order.customer_id ? (
                <Link to={`/app/customers/${order.customer_id}`} className="hover:underline">
                  Customer
                </Link>
              ) : (
                "Guest order"
              )}
            </p>
          </div>
          <Badge variant={isDraft ? "secondary" : "default"}>{order.status}</Badge>
        </div>

        <section className="flex flex-col gap-2 text-sm">
          <h2 className="text-lg font-semibold">Fulfillment</h2>
          <dl className="flex flex-col gap-1">
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Date</dt>
              <dd>{order.fulfillment_date ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Time</dt>
              <dd>{order.fulfillment_time ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Method</dt>
              <dd>{order.fulfillment_method ?? "—"}</dd>
            </div>
          </dl>
          {order.fulfillment_details && (
            <div>
              <p className="text-xs text-muted-foreground">Details</p>
              <p className="whitespace-pre-wrap">{order.fulfillment_details}</p>
            </div>
          )}
          {order.fulfillment_notes && (
            <div>
              <p className="text-xs text-muted-foreground">Fulfillment-facing notes</p>
              <p className="whitespace-pre-wrap">{order.fulfillment_notes}</p>
            </div>
          )}
        </section>

        {order.internal_notes && (
          <section className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
            <h2 className="mb-1 font-semibold">Internal notes (not customer-facing)</h2>
            <p className="whitespace-pre-wrap">{order.internal_notes}</p>
          </section>
        )}

        <section className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">Order Items</h2>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Item</TableHead>
                <TableHead>Qty</TableHead>
                <TableHead>Price</TableHead>
                <TableHead>Subtotal</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {order.lines.map((line) => (
                <TableRow key={line.id}>
                  <TableCell>
                    {line.display_name_snapshot}
                    {line.notes && <p className="text-xs text-muted-foreground">{line.notes}</p>}
                    {/* Internal-only Custom Item detail — visually separate (muted,
                        boxed) from the customer-facing Qty/Price/Subtotal columns
                        (Checkpoint-3 correction 18). No manual_fulfillment_satisfied
                        control — Phase 6 never exposes one. */}
                    {line.line_type === "CUSTOM_ITEM" && (
                      <div className="mt-1 flex flex-wrap gap-1">
                        <Badge variant={line.manual_fulfillment_required ? "outline" : "secondary"}>
                          {line.manual_fulfillment_required
                            ? "Manual fulfillment required"
                            : "No manual fulfillment"}
                        </Badge>
                        {line.custom_direct_cost_estimate && (
                          <Badge variant="outline">
                            Internal cost est. ${line.custom_direct_cost_estimate}
                          </Badge>
                        )}
                        {line.custom_active_time_minutes != null && (
                          <Badge variant="outline">
                            Est. {line.custom_active_time_minutes} min active time
                          </Badge>
                        )}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    {line.line_type === "STANDARD_OPTION"
                      ? `${formatQuantityForDisplay(line.package_quantity)} pkg`
                      : formatQuantityForDisplay(line.underlying_quantity)}
                  </TableCell>
                  <TableCell>${line.charged_unit_price_snapshot}</TableCell>
                  <TableCell>${line.line_subtotal}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="text-lg font-semibold">Payments</h2>
          {order.payments.length === 0 ? (
            <p className="text-sm text-muted-foreground">No payments recorded yet.</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Date</TableHead>
                  <TableHead>Method</TableHead>
                  <TableHead>Amount</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {order.payments.map((payment) => (
                  <TableRow key={payment.id}>
                    <TableCell>{payment.payment_date}</TableCell>
                    <TableCell>{payment.payment_method}</TableCell>
                    <TableCell>${payment.amount}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          <div className="flex items-center gap-2 text-sm">
            <Badge variant={order.payment_status === "PAID" ? "default" : "secondary"}>
              {PAYMENT_STATUS_LABEL[order.payment_status]}
            </Badge>
            {order.overpayment_amount && (
              <span className="text-amber-600">Overpaid by ${order.overpayment_amount}</span>
            )}
          </div>

          {isCanceled ? (
            <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
              New payments cannot be recorded on a canceled order.
            </p>
          ) : (
            <form className="flex flex-col gap-2 rounded-md border p-3" onSubmit={submitPayment}>
              <h3 className="text-sm font-semibold">Add Payment</h3>
              <FormField id="amount" label="Amount" error={paymentErrors.amount?.message}>
                <CurrencyInput id="amount" {...register("amount")} />
              </FormField>
              <FormField
                id="payment_method"
                label="Payment method"
                error={paymentErrors.payment_method?.message}
              >
                <Input id="payment_method" {...register("payment_method")} />
              </FormField>
              <FormField
                id="payment_date"
                label="Payment date"
                error={paymentErrors.payment_date?.message}
              >
                <Input id="payment_date" type="date" {...register("payment_date")} />
              </FormField>
              <FormField id="notes" label="Notes">
                <Input id="notes" {...register("notes")} />
              </FormField>

              {overpaymentIssue && (
                <div className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-2 text-sm">
                  <p role="alert">{overpaymentIssue.message}</p>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={handleSubmit((values) =>
                      addPaymentMutation.mutate({ ...values, confirm_overpayment: true }),
                    )}
                  >
                    Confirm overpayment
                  </Button>
                </div>
              )}

              {addPaymentGenericError && (
                <p role="alert" className="text-sm text-destructive">
                  {addPaymentGenericError}
                </p>
              )}

              <Button type="submit" size="sm" disabled={addPaymentMutation.isPending}>
                Add Payment
              </Button>
            </form>
          )}
        </section>

        {isDraft && (
          <section className="rounded-md border border-dashed p-3 text-sm">
            <h2 className="mb-1 font-semibold">Confirmation</h2>
            {order.is_confirmable ? (
              <p className="mb-2 text-green-700">✓ Structurally ready for confirmation.</p>
            ) : (
              <ul className="mb-2 list-inside list-disc text-muted-foreground">
                {order.confirmation_issues.map((issue) => (
                  <li key={issue.code}>{issue.message}</li>
                ))}
              </ul>
            )}

            {confirmWarningIssues ? (
              <div className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3">
                <p className="font-semibold">Review before confirming</p>
                <ul className="list-inside list-disc">
                  {confirmWarningIssues.map((issue, index) => (
                    <li key={`${issue.code}-${index}`} role="alert">
                      {issue.message}
                    </li>
                  ))}
                </ul>
                <Button
                  type="button"
                  size="sm"
                  onClick={handleConfirmAnyway}
                  disabled={confirmMutation.isPending}
                >
                  Confirm anyway
                </Button>
              </div>
            ) : (
              <>
                {confirmGenericError && (
                  <p role="alert" className="mb-2 text-sm text-destructive">
                    {confirmGenericError}
                  </p>
                )}
                <Button
                  type="button"
                  size="sm"
                  disabled={!order.is_confirmable || confirmMutation.isPending}
                  onClick={() => confirmMutation.mutate([])}
                >
                  Confirm Order
                </Button>
              </>
            )}
          </section>
        )}

        {order.status === "CONFIRMED" && (
          <section className="rounded-md border border-dashed p-3 text-sm">
            <h2 className="mb-1 font-semibold">Confirmed</h2>
            {order.production_locked ? (
              <p className="mb-2 text-amber-700">
                Some of this order's demand is covered by an active production run. It cannot be
                canceled until that run completes or is canceled, and its operational fields
                (quantity, product, date, time) are read-only here for the same reason — pricing
                and notes can still be edited. The server independently re-checks this for every
                change, regardless of what this notice reports.
              </p>
            ) : null}
            {cancelGenericError && (
              <p role="alert" className="mb-2 text-sm text-destructive">
                {cancelGenericError}
              </p>
            )}
            <div className="flex items-center gap-2">
              <Link
                to={`/app/orders/${order.id}/edit`}
                className={buttonVariants({ variant: "outline", size: "sm" })}
              >
                Edit
              </Link>
              <AlertDialog>
                <AlertDialogTrigger
                  className={buttonVariants({ variant: "destructive", size: "sm" })}
                  disabled={order.production_locked}
                >
                  Cancel Order
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Cancel this order?</AlertDialogTitle>
                    <AlertDialogDescription>
                      This releases any reserved ingredients, purchased stock, and surplus
                      allocated to it. This cannot be undone.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Keep order</AlertDialogCancel>
                    <AlertDialogAction
                      className={buttonVariants({ variant: "destructive" })}
                      onClick={() => cancelMutation.mutate()}
                    >
                      Cancel order
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            </div>
          </section>
        )}

        {staleVersionOccurred && <StaleVersionPanel onRefresh={handleRefreshAfterStaleVersion} />}

        {deleteGenericError && (
          <p role="alert" className="text-sm text-destructive">
            {deleteGenericError}
          </p>
        )}

        {isDraft && (
          <div className="flex items-center gap-2">
            <Link
              to={`/app/orders/${order.id}/edit`}
              className={buttonVariants({ variant: "outline" })}
            >
              Edit
            </Link>

            {paymentsWarning ? (
              <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
                <p role="alert">{paymentsWarning.message}</p>
                <Button
                  type="button"
                  variant="destructive"
                  size="sm"
                  onClick={() => deleteMutation.mutate(true)}
                >
                  Delete anyway
                </Button>
              </div>
            ) : (
              <AlertDialog>
                <AlertDialogTrigger className={buttonVariants({ variant: "destructive" })}>
                  Delete
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Delete this draft order?</AlertDialogTitle>
                    <AlertDialogDescription>
                      This cannot be undone. If it has recorded payments, you will be warned before
                      they are deleted too.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction
                      className={buttonVariants({ variant: "destructive" })}
                      onClick={() => deleteMutation.mutate(false)}
                    >
                      Delete
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            )}
          </div>
        )}
      </div>

      <aside className="flex flex-col gap-4 lg:sticky lg:top-4 lg:self-start">
        <div className="rounded-md border p-4">
          <h2 className="mb-2 text-sm font-semibold">Customer-facing total</h2>
          <dl className="flex flex-col gap-1 text-sm">
            <div className="flex justify-between">
              <dt>Subtotal</dt>
              <dd>${order.subtotal}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Adjustment</dt>
              <dd>${order.order_adjustment}</dd>
            </div>
            <div className="flex justify-between">
              <dt>Tax</dt>
              <dd>${order.manual_tax}</dd>
            </div>
            <div className="flex justify-between font-medium">
              <dt>Total</dt>
              <dd>${order.final_total}</dd>
            </div>
          </dl>
        </div>
        <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
          <h2 className="mb-1 font-semibold">Internal estimate</h2>
          <p>Not yet calculated — available in a future phase.</p>
        </div>
      </aside>
    </div>
  );
}
