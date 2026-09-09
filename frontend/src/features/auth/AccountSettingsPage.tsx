// Minimal account settings UI (Phase 2 plan §2): authenticated password change, and full
// account/business deletion behind current-password + exact business-name confirmation
// (Phase 2 plan §7). Business Settings CRUD (name/timezone/etc.) is out of Phase 2 scope.

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { useNavigate } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { Button } from "../../components/ui/button";
import { authApi } from "./api";
import { useAuth } from "./AuthContext";

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
      {mutation.isSuccess && <p className="text-sm">Password changed.</p>}

      <Button type="submit" disabled={mutation.isPending} className="w-fit">
        {mutation.isPending ? "Changing…" : "Change password"}
      </Button>
    </form>
  );
}

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

      <Button
        type="submit"
        variant="destructive"
        disabled={mutation.isPending}
        className="w-fit"
      >
        {mutation.isPending ? "Deleting…" : "Delete account"}
      </Button>
    </form>
  );
}

export function AccountSettingsPage() {
  const { me } = useAuth();

  return (
    <div className="flex flex-col gap-10">
      <h1 className="text-xl font-semibold">Account</h1>

      <section className="flex flex-col gap-4">
        <h2 className="text-sm font-semibold text-muted-foreground">Change password</h2>
        <ChangePasswordForm />
      </section>

      {me && (
        <section className="flex flex-col gap-4">
          <h2 className="text-sm font-semibold text-destructive">Delete account</h2>
          <DeleteAccountForm businessName={me.business.name} />
        </section>
      )}
    </div>
  );
}
