// Create/edit Customer (Phase 3 plan v3 §15). Dedicated route, not a modal. Duplicate
// warning (§11) shown as an inline consolidated-review panel, not a toast/sequential
// modal (Spec §12.8). Edit mode round-trips `version` for optimistic-concurrency (§4).

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { useNavigate, useParams } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { FormField } from "../../components/shared/FormField";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import { customersApi, type Customer } from "./api";

const schema = z.object({
  name: z.string().min(1, "Name is required"),
  phone: z.string().optional(),
  email: z.string().optional(),
  preferred_contact_method: z.string().optional(),
  notes: z.string().optional(),
});

type FormValues = z.infer<typeof schema>;

function toFormValues(customer?: Customer): FormValues {
  return {
    name: customer?.name ?? "",
    phone: customer?.phone ?? "",
    email: customer?.email ?? "",
    preferred_contact_method: customer?.preferred_contact_method ?? "",
    notes: customer?.notes ?? "",
  };
}

function toPayload(values: FormValues) {
  return {
    name: values.name,
    phone: values.phone || null,
    email: values.email || null,
    preferred_contact_method: values.preferred_contact_method || null,
    notes: values.notes || null,
  };
}

export function CustomerFormPage() {
  const { id } = useParams<{ id: string }>();
  const isEdit = Boolean(id);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const existingQuery = useQuery({
    queryKey: ["customers", id],
    queryFn: () => customersApi.get(id as string),
    enabled: isEdit,
    // A 404 (missing or foreign-tenant) won't resolve on retry — matches the Detail page.
    retry: false,
  });

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema), defaultValues: toFormValues() });

  useEffect(() => {
    if (existingQuery.data) {
      reset(toFormValues(existingQuery.data));
    }
  }, [existingQuery.data, reset]);

  const mutation = useMutation({
    mutationFn: ({
      values,
      confirmDuplicate,
    }: {
      values: FormValues;
      confirmDuplicate: boolean;
    }) => {
      const payload = toPayload(values);
      // Checkpoint 3 remediation: in edit mode this must NEVER silently fall through to
      // create just because `existingQuery.data` happens to be absent — that path is
      // supposed to be unreachable because the render guards below bail out before the
      // form (and therefore this mutation) ever renders when the prerequisite GET failed.
      // This throw is a defense-in-depth backstop, not the primary guard.
      if (isEdit) {
        if (!existingQuery.data) {
          throw new Error("Cannot save: the original record failed to load.");
        }
        return customersApi.update(existingQuery.data.id, {
          version: existingQuery.data.version,
          ...payload,
        });
      }
      return customersApi.create({ ...payload, confirm_duplicate: confirmDuplicate });
    },
    onSuccess: async (customer) => {
      await queryClient.invalidateQueries({ queryKey: ["customers"] });
      navigate(`/app/customers/${customer.id}`);
    },
  });

  const submit = handleSubmit((values) => mutation.mutate({ values, confirmDuplicate: false }));
  const submitConfirmingDuplicate = handleSubmit((values) =>
    mutation.mutate({ values, confirmDuplicate: true }),
  );

  const duplicateIssue =
    mutation.error instanceof ApiError
      ? mutation.error.body?.error.issues.find((i) => i.code === "POSSIBLE_DUPLICATE_CUSTOMER")
      : undefined;
  const isStaleVersion =
    mutation.error instanceof ApiError && mutation.error.body?.error.code === "STALE_VERSION";

  const handleRefreshAfterStaleVersion = async () => {
    mutation.reset();
    const fresh = await existingQuery.refetch();
    if (fresh.data) {
      reset(toFormValues(fresh.data));
    }
  };

  // --- Edit-mode prerequisite-GET guards (Checkpoint 3 remediation) — these must run
  // BEFORE the form renders at all, so a failed fetch can never fall through to a form
  // whose submit handler would otherwise attempt to create instead of update.
  if (isEdit && existingQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }

  if (isEdit && existingQuery.isError) {
    const isNotFound = existingQuery.error instanceof ApiError && existingQuery.error.status === 404;
    if (isNotFound) {
      return (
        <div className="flex flex-col gap-2">
          <h1 className="text-xl font-semibold">Customer not found</h1>
          <Button variant="outline" onClick={() => navigate("/app/customers")}>
            Back to customers
          </Button>
        </div>
      );
    }
    return <ErrorBanner error={existingQuery.error} onRetry={() => existingQuery.refetch()} />;
  }

  return (
    <div className="mx-auto flex max-w-md flex-col gap-6">
      <h1 className="text-xl font-semibold">{isEdit ? "Edit Customer" : "New Customer"}</h1>

      <form className="flex flex-col gap-4" onSubmit={submit}>
        <FormField id="name" label="Name" error={errors.name?.message}>
          <Input id="name" {...register("name")} />
        </FormField>
        <FormField id="phone" label="Phone" error={errors.phone?.message}>
          <Input id="phone" {...register("phone")} />
        </FormField>
        <FormField id="email" label="Email" error={errors.email?.message}>
          <Input id="email" type="email" {...register("email")} />
        </FormField>
        <FormField
          id="preferred_contact_method"
          label="Preferred contact method"
          error={errors.preferred_contact_method?.message}
        >
          <Input id="preferred_contact_method" {...register("preferred_contact_method")} />
        </FormField>
        <FormField id="notes" label="Notes" error={errors.notes?.message}>
          <Textarea id="notes" {...register("notes")} />
        </FormField>

        {duplicateIssue && (
          <div className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
            <p role="alert">{duplicateIssue.message}</p>
            <Button type="button" variant="outline" size="sm" onClick={submitConfirmingDuplicate}>
              Create anyway
            </Button>
          </div>
        )}

        {isStaleVersion && (
          <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
            <p role="alert">
              This record changed since you opened it. Refresh the latest version and
              review your changes before saving again.
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={handleRefreshAfterStaleVersion}
            >
              Refresh
            </Button>
          </div>
        )}

        {mutation.isError && !duplicateIssue && !isStaleVersion && (
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
    </div>
  );
}
