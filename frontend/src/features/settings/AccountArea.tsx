// Account area of Settings (Spec AUTH-005/007/008): the owner's display name, the read-only
// login email, authenticated password change, and full account deletion behind current
// password + exact business-name confirmation.
//
// Phase 11 scope: the owner edits their *name* only. The login email is shown but cannot be
// changed (no email-change/verification workflow exists), and the password flow is the
// unchanged Phase 2 one, relocated here from the former standalone Account page.

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm, useWatch } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { FormField } from "../../components/shared/FormField";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { authApi, type MeResponse } from "../auth/api";
import { ME_QUERY_KEY, useAuth } from "../auth/AuthContext";
import { settingsApi } from "./api";
import { applyServerFieldErrors, errorMessage } from "./shared";

// --------------------------------------------------------------------------- owner profile

const ownerSchema = z.object({ name: z.string().trim().min(1, "Name is required") });
type OwnerValues = z.infer<typeof ownerSchema>;

function OwnerProfileForm({ name, email }: { name: string; email: string }) {
  const queryClient = useQueryClient();
  const {
    register,
    handleSubmit,
    reset,
    setError,
    control,
    formState: { errors },
  } = useForm<OwnerValues>({ resolver: zodResolver(ownerSchema), defaultValues: { name } });

  const mutation = useMutation({
    mutationFn: (value: string) => settingsApi.updateOwner(value),
    onSuccess: (saved) => {
      queryClient.setQueryData<MeResponse>(ME_QUERY_KEY, (old) =>
        old ? { ...old, user: { ...old.user, name: saved.name } } : old,
      );
      void queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
      reset({ name: saved.name });
    },
    onError: (error) => applyServerFieldErrors(error, setError, ["name"]),
  });

  const typedName = useWatch({ control, name: "name" });
  const dirty = (typedName ?? "").trim() !== name;

  return (
    <form
      className="flex max-w-sm flex-col gap-4"
      noValidate
      onSubmit={handleSubmit((values) => mutation.mutate(values.name.trim()))}
    >
      <FormField id="owner_name" label="Your name" error={errors.name?.message}>
        <Input id="owner_name" autoComplete="name" {...register("name")} />
      </FormField>
      <div className="flex flex-col gap-1">
        <span className="text-sm font-medium">Login email</span>
        <p className="text-sm" data-testid="owner-email">
          {email}
        </p>
        <p className="text-xs text-muted-foreground">Your login email can't be changed here.</p>
      </div>
      {mutation.isError &&
        !(mutation.error instanceof ApiError && mutation.error.status === 422) && (
          <p role="alert" className="text-sm text-destructive">
            {errorMessage(mutation.error)}
          </p>
        )}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={mutation.isPending || !dirty} className="w-fit">
          {mutation.isPending ? "Saving…" : "Save name"}
        </Button>
        {mutation.isSuccess && !dirty && (
          <p role="status" className="text-sm text-muted-foreground">
            Saved
          </p>
        )}
      </div>
    </form>
  );
}

// ------------------------------------------------------------------------ change password

const changePasswordSchema = z.object({
  current_password: z.string().min(1, "Current password is required"),
  new_password: z
    .string()
    .min(15, "Must be at least 15 characters")
    .max(128, "Must be at most 128 characters"),
});
type ChangePasswordValues = z.infer<typeof changePasswordSchema>;

function ChangePasswordForm() {
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<ChangePasswordValues>({ resolver: zodResolver(changePasswordSchema) });

  const mutation = useMutation({
    mutationFn: authApi.changePassword,
    onSuccess: () => reset(),
  });

  return (
    <form
      className="flex max-w-sm flex-col gap-4"
      onSubmit={handleSubmit((values) => mutation.mutate(values))}
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="current_password" className="text-sm font-medium">
          Current password
        </label>
        <input
          id="current_password"
          type="password"
          autoComplete="current-password"
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
          {...register("current_password")}
        />
        {errors.current_password && (
          <p className="text-xs text-destructive">{errors.current_password.message}</p>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="new_password" className="text-sm font-medium">
          New password
        </label>
        <input
          id="new_password"
          type="password"
          autoComplete="new-password"
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
          {...register("new_password")}
        />
        {errors.new_password && (
          <p className="text-xs text-destructive">{errors.new_password.message}</p>
        )}
      </div>

      {mutation.isError && (
        <p role="alert" className="text-sm text-destructive">
          {mutation.error instanceof ApiError
            ? mutation.error.body?.error.message
            : "Something went wrong. Please try again."}
        </p>
      )}
      {mutation.isSuccess && (
        <p role="status" className="text-sm">
          Password changed.
        </p>
      )}

      <Button type="submit" disabled={mutation.isPending} className="w-fit">
        {mutation.isPending ? "Changing…" : "Change password"}
      </Button>
    </form>
  );
}

// ------------------------------------------------------------------------ delete account

function deleteAccountSchema(businessName: string) {
  return z.object({
    current_password: z.string().min(1, "Current password is required"),
    business_name_confirmation: z
      .string()
      .refine((value) => value === businessName, `Type "${businessName}" exactly to confirm`),
  });
}
type DeleteAccountValues = z.infer<ReturnType<typeof deleteAccountSchema>>;

function DeleteAccountForm({ businessName }: { businessName: string }) {
  const navigate = useNavigate();
  const { clearMe } = useAuth();
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<DeleteAccountValues>({ resolver: zodResolver(deleteAccountSchema(businessName)) });

  const mutation = useMutation({
    mutationFn: authApi.deleteAccount,
    onSuccess: () => {
      // clearMe() resets the auth query; AuthProvider then purges every tenant query once
      // the principal is gone (tenant-cache boundary), so nothing of this Business survives.
      clearMe();
      navigate("/login", { replace: true });
    },
  });

  return (
    <form
      className="flex max-w-sm flex-col gap-4"
      onSubmit={handleSubmit((values) => mutation.mutate(values))}
    >
      <p className="text-sm text-muted-foreground">
        This permanently deletes your account and business. This cannot be undone.
      </p>
      <div className="flex flex-col gap-1">
        <label htmlFor="delete_current_password" className="text-sm font-medium">
          Current password
        </label>
        <input
          id="delete_current_password"
          type="password"
          autoComplete="current-password"
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
          {...register("current_password")}
        />
        {errors.current_password && (
          <p className="text-xs text-destructive">{errors.current_password.message}</p>
        )}
      </div>
      <div className="flex flex-col gap-1">
        <label htmlFor="business_name_confirmation" className="text-sm font-medium">
          Type "{businessName}" to confirm
        </label>
        <input
          id="business_name_confirmation"
          className="rounded-md border border-input bg-background px-3 py-2 text-sm"
          {...register("business_name_confirmation")}
        />
        {errors.business_name_confirmation && (
          <p className="text-xs text-destructive">{errors.business_name_confirmation.message}</p>
        )}
      </div>

      {mutation.isError && (
        <p role="alert" className="text-sm text-destructive">
          {mutation.error instanceof ApiError
            ? mutation.error.body?.error.message
            : "Something went wrong. Please try again."}
        </p>
      )}

      <Button type="submit" variant="destructive" disabled={mutation.isPending} className="w-fit">
        {mutation.isPending ? "Deleting…" : "Delete account"}
      </Button>
    </form>
  );
}

// ------------------------------------------------------------------------------- the area

export function AccountArea() {
  const { me } = useAuth();

  return (
    <div className="flex flex-col gap-10">
      {me && (
        <section aria-labelledby="owner-profile-heading" className="flex flex-col gap-4">
          <h3 id="owner-profile-heading" className="text-sm font-semibold text-muted-foreground">
            Owner profile
          </h3>
          <OwnerProfileForm name={me.user.name} email={me.user.email} />
        </section>
      )}

      <section aria-labelledby="change-password-heading" className="flex flex-col gap-4">
        <h3 id="change-password-heading" className="text-sm font-semibold text-muted-foreground">
          Change password
        </h3>
        <ChangePasswordForm />
      </section>

      {me && (
        <section aria-labelledby="delete-account-heading" className="flex flex-col gap-4">
          <h3 id="delete-account-heading" className="text-sm font-semibold text-destructive">
            Delete account
          </h3>
          <DeleteAccountForm businessName={me.business.name} />
        </section>
      )}
    </div>
  );
}
