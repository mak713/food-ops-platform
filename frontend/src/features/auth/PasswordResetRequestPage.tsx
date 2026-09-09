import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link } from "react-router-dom";
import { z } from "zod";
import { Button } from "../../components/ui/button";
import { authApi } from "./api";

const schema = z.object({ email: z.string().min(1, "Email is required") });
type FormValues = z.infer<typeof schema>;

export function PasswordResetRequestPage() {
  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  const mutation = useMutation({
    mutationFn: (values: FormValues) => authApi.requestPasswordReset(values.email),
  });

  return (
    <div className="mx-auto flex min-h-svh max-w-sm flex-col justify-center gap-6 p-8">
      <h1 className="text-xl font-semibold">Reset your password</h1>

      {mutation.isSuccess ? (
        // Deliberately generic, regardless of whether the email is registered (Spec §10.7).
        <p className="text-sm">
          If that email is registered, a reset link has been sent to it. Follow the link there to
          finish resetting your password.
        </p>
      ) : (
        <form
          className="flex flex-col gap-4"
          onSubmit={handleSubmit((values) => mutation.mutate(values))}
        >
          <div className="flex flex-col gap-1">
            <label htmlFor="email" className="text-sm font-medium">
              Email
            </label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              className="rounded-md border border-input bg-background px-3 py-2 text-sm"
              {...register("email")}
            />
            {errors.email && <p className="text-xs text-destructive">{errors.email.message}</p>}
          </div>
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? "Sending…" : "Send reset link"}
          </Button>
        </form>
      )}

      <Link to="/login" className="text-sm text-muted-foreground underline underline-offset-4">
        Back to login
      </Link>
    </div>
  );
}
