// One Selling Option row on ProductDetailPage: view mode, or an inline edit form when
// selected. Editable/archivable regardless of the parent Product's active state (Phase 3
// plan v3 §7) — never disabled based on `product.is_active`.

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { ApiError } from "../../api/client";
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
import { TableCell, TableRow } from "../../components/ui/table";
import { sellingOptionsApi, type Product, type SellingOption } from "./api";

// Checkpoint 3 remediation: real numeric validation, not just "non-empty string" — a
// value like "0" or "-1" or "abc" must be blocked client-side before any fetch, matching
// the backend's own constraints (quantity_units > 0; price/packaging_cost >= 0; sort_order
// a valid integer). `Number(v)` (not `parseInt`) so trailing garbage like "12abc" is
// rejected outright instead of silently truncated to 12.
const schema = z.object({
  name: z.string().min(1, "Name is required"),
  quantity_units: z
    .string()
    .min(1, "Quantity is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) > 0, "Quantity must be greater than 0"),
  price: z
    .string()
    .min(1, "Price is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) >= 0, "Price must be 0 or greater"),
  packaging_cost: z
    .string()
    .optional()
    .refine(
      (v) => !v || (!Number.isNaN(Number(v)) && Number(v) >= 0),
      "Packaging cost must be 0 or greater",
    ),
  sort_order: z
    .string()
    .optional()
    .refine((v) => !v || Number.isInteger(Number(v)), "Sort order must be a whole number"),
});

type FormValues = z.infer<typeof schema>;

// Checkpoint 3 manual-testing remediation: shared by the form's initial `defaultValues`
// and by the stale-version Refresh handler below, so both derive a row's form state from
// a SellingOption the exact same way.
function toFormValues(option: Pick<SellingOption, "name" | "quantity_units" | "price" | "packaging_cost" | "sort_order">): FormValues {
  return {
    name: option.name,
    quantity_units: option.quantity_units,
    price: option.price,
    packaging_cost: option.packaging_cost,
    sort_order: String(option.sort_order),
  };
}

interface SellingOptionRowProps {
  productId: string;
  option: SellingOption;
}

export function SellingOptionRow({ productId, option }: SellingOptionRowProps) {
  const [isEditing, setIsEditing] = useState(false);
  // Final pre-freeze remediation: the version an in-progress edit session was opened
  // against, captured once when Edit is clicked — deliberately NOT read live off the
  // `option` prop at submit time. `option` can advance (a refetch triggered by some other
  // action while this row's form stays mounted) without this row's form being resynced;
  // if the PATCH used the live prop's version instead, it could pair a stale/unrelated
  // edit-session's field values with a newer version number and slip past the optimistic-
  // concurrency check the server is supposed to enforce. Seeding from `option.version` is
  // only a harmless initial value — view mode never reads this state, and every entry into
  // edit mode (including after Cancel) overwrites it via the Edit button below.
  const [editSessionVersion, setEditSessionVersion] = useState(option.version);
  const queryClient = useQueryClient();

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["products", productId] });

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: toFormValues(option),
  });

  const updateMutation = useMutation({
    mutationFn: (values: FormValues) =>
      sellingOptionsApi.update(productId, option.id, {
        version: editSessionVersion,
        name: values.name,
        quantity_units: values.quantity_units,
        price: values.price,
        packaging_cost: values.packaging_cost || "0",
        // Safe now: Zod already validated this is blank or a genuine integer string.
        sort_order: values.sort_order ? Number(values.sort_order) : 0,
      }),
    onSuccess: async () => {
      await invalidate();
      setIsEditing(false);
    },
  });

  const archiveMutation = useMutation({
    mutationFn: () => sellingOptionsApi.archive(productId, option.id, option.version),
    onSuccess: invalidate,
  });
  const reactivateMutation = useMutation({
    mutationFn: () => sellingOptionsApi.reactivate(productId, option.id, option.version),
    onSuccess: invalidate,
  });
  const deleteMutation = useMutation({
    mutationFn: () => sellingOptionsApi.remove(productId, option.id, option.version),
    onSuccess: invalidate,
  });

  const isStale = (err: unknown) =>
    err instanceof ApiError && err.body?.error.code === "STALE_VERSION";

  // Checkpoint 3 remediation: a real Refresh action — refetches the parent Product (which
  // is what re-supplies this row's `option` prop with the current version) and clears
  // every mutation's stale error state, discarding whatever the user had typed in the
  // inline edit form rather than letting them resubmit against a version that's gone.
  //
  // Manual-testing remediation: `invalidate()` alone isn't enough to fix what the *inline
  // edit form* displays — react-hook-form's `defaultValues` are only read once, at mount,
  // and this row's component instance is never remounted just because its `option` prop
  // changes (same `key`). Without an explicit `reset(...)`, the form keeps showing
  // whatever the user had typed before the rejected save, even after the authoritative
  // data has been refetched. `invalidateQueries` awaits its refetch, so the query cache
  // already holds the fresh Product by the time we read it back out here.
  const handleRefreshAfterStaleVersion = async () => {
    updateMutation.reset();
    archiveMutation.reset();
    reactivateMutation.reset();
    deleteMutation.reset();
    await invalidate();
    const freshProduct = queryClient.getQueryData<Product>(["products", productId]);
    const freshOption = freshProduct?.selling_options.find((o) => o.id === option.id);
    reset(toFormValues(freshOption ?? option));
    setIsEditing(false);
  };

  if (isEditing) {
    const isUpdateStale = isStale(updateMutation.error);
    return (
      <TableRow>
        <TableCell colSpan={5}>
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={handleSubmit((values) => updateMutation.mutate(values))}
          >
            <div className="flex flex-col gap-1">
              <label htmlFor={`option-${option.id}-name`} className="text-xs text-muted-foreground">
                Name
              </label>
              <Input id={`option-${option.id}-name`} className="w-32" {...register("name")} />
              {errors.name && <p className="text-xs text-destructive">{errors.name.message}</p>}
            </div>
            <div className="flex flex-col gap-1">
              <label
                htmlFor={`option-${option.id}-quantity`}
                className="text-xs text-muted-foreground"
              >
                Quantity
              </label>
              <Input
                id={`option-${option.id}-quantity`}
                className="w-24"
                inputMode="decimal"
                {...register("quantity_units")}
              />
              {errors.quantity_units && (
                <p className="text-xs text-destructive">{errors.quantity_units.message}</p>
              )}
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor={`option-${option.id}-price`} className="text-xs text-muted-foreground">
                Price
              </label>
              <Input
                id={`option-${option.id}-price`}
                className="w-24"
                inputMode="decimal"
                {...register("price")}
              />
              {errors.price && <p className="text-xs text-destructive">{errors.price.message}</p>}
            </div>
            <div className="flex flex-col gap-1">
              <label
                htmlFor={`option-${option.id}-packaging-cost`}
                className="text-xs text-muted-foreground"
              >
                Packaging cost
              </label>
              <Input
                id={`option-${option.id}-packaging-cost`}
                className="w-24"
                inputMode="decimal"
                {...register("packaging_cost")}
              />
              {errors.packaging_cost && (
                <p className="text-xs text-destructive">{errors.packaging_cost.message}</p>
              )}
            </div>
            <div className="flex flex-col gap-1">
              <label
                htmlFor={`option-${option.id}-sort-order`}
                className="text-xs text-muted-foreground"
              >
                Sort order
              </label>
              <Input
                id={`option-${option.id}-sort-order`}
                className="w-16"
                inputMode="numeric"
                {...register("sort_order")}
              />
              {errors.sort_order && (
                <p className="text-xs text-destructive">{errors.sort_order.message}</p>
              )}
            </div>
            <Button type="submit" size="sm" disabled={updateMutation.isPending}>
              {updateMutation.isPending ? "Saving…" : "Save"}
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setIsEditing(false)}>
              Cancel
            </Button>
          </form>
          {isUpdateStale ? (
            <div className="mt-1 flex items-center gap-2 text-xs text-destructive">
              <p role="alert">This record changed since you opened it.</p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={handleRefreshAfterStaleVersion}
              >
                Refresh
              </Button>
            </div>
          ) : (
            updateMutation.isError && (
              <p role="alert" className="mt-1 text-xs text-destructive">
                {updateMutation.error instanceof ApiError
                  ? updateMutation.error.body?.error.message
                  : "Something went wrong."}
              </p>
            )
          )}
        </TableCell>
      </TableRow>
    );
  }

  const actionError = archiveMutation.error ?? reactivateMutation.error ?? deleteMutation.error;
  const staleVersionOccurred =
    isStale(archiveMutation.error) || isStale(reactivateMutation.error) || isStale(deleteMutation.error);

  return (
    <TableRow>
      <TableCell>{option.name}</TableCell>
      <TableCell>{option.quantity_units}</TableCell>
      <TableCell>{option.price}</TableCell>
      <TableCell>
        <Badge variant={option.is_active ? "default" : "secondary"}>
          {option.is_active ? "Active" : "Archived"}
        </Badge>
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap items-center gap-1">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              // Final pre-freeze remediation: explicitly (re)sync the form and the
              // edit-session version from whatever `option` is current right now — never
              // relying on stale `defaultValues`/state left over from a previous edit
              // session (e.g. after Cancel, or if props advanced while this row sat in
              // view mode).
              reset(toFormValues(option));
              setEditSessionVersion(option.version);
              setIsEditing(true);
            }}
          >
            Edit
          </Button>
          {option.is_active ? (
            <Button variant="outline" size="sm" onClick={() => archiveMutation.mutate()}>
              Archive
            </Button>
          ) : (
            <Button variant="outline" size="sm" onClick={() => reactivateMutation.mutate()}>
              Reactivate
            </Button>
          )}
          <AlertDialog>
            <AlertDialogTrigger className={buttonVariants({ variant: "destructive", size: "sm" })}>
              Delete
            </AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Delete this selling option?</AlertDialogTitle>
                <AlertDialogDescription>
                  This cannot be undone. If it has order history, deletion will be blocked —
                  archive it instead.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction
                  className={buttonVariants({ variant: "destructive" })}
                  onClick={() => deleteMutation.mutate()}
                >
                  Delete
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </div>
        {staleVersionOccurred ? (
          <div className="mt-1 flex items-center gap-2 text-xs text-destructive">
            <p role="alert">This record changed since you opened it.</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleRefreshAfterStaleVersion}
            >
              Refresh
            </Button>
          </div>
        ) : (
          actionError && (
            <p role="alert" className="mt-1 text-xs text-destructive">
              {actionError instanceof ApiError
                ? actionError.body?.error.message
                : "Something went wrong."}
            </p>
          )
        )}
      </TableCell>
    </TableRow>
  );
}
