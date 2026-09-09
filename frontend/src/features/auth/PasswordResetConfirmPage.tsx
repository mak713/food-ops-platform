import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { Button } from "../../components/ui/button";
import { authApi } from "./api";

const schema = z.object({
  token: z.string().min(1, "Reset token is required"),
  new_password: z
    .string()
    .min(15, "Must be at least 15 characters")
    .max(128, "Must be at most 128 characters"),
});
type FormValues = z.infer<typeof schema>;

export function PasswordResetConfirmPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { token: searchParams.get("token") ?? "" },
  });

  const mutation = useMutation({
    mutationFn: (values: FormValues) =>
      authApi.confirmPasswordReset(values.token, values.new_password),
    onSuccess: () => navigate("/login", { replace: true }),
  });

  return (
    <div className="mx-auto flex min-h-svh max-w-sm flex-col justify-center gap-6 p-8">
      <h1 className="text-xl font-semibold">Choose a new password</h1>
      <form
        className="flex flex-col gap-4"
        onSubmit={handleSubmit((values) => mutation.mutate(values))}
      >
        <div className="flex flex-col gap-1">
          <label htmlFor="token" className="text-sm font-medium">
            Reset token
          </label>
          <input
            id="token"
            className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            {...register("token")}
          />
          {errors.token && <p className="text-xs text-destructive">{errors.token.message}</p>}
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

        <Button type="submit" disabled={mutation.isPending}>
          {mutation.isPending ? "Resetting…" : "Reset password"}
        </Button>
      </form>
      <Link to="/login" className="text-sm text-muted-foreground underline underline-offset-4">
        Back to login
      </Link>
    </div>
  );
}
