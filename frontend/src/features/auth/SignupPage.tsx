import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { Button } from "../../components/ui/button";
import { authApi } from "./api";
import { useAuth } from "./AuthContext";

// Mirrors the backend password policy (backend/app/core/security.py,
// PASSWORD_MIN_LENGTH/PASSWORD_MAX_LENGTH) — no composition-class rules, passphrases and
// spaces allowed, never truncated. The backend remains authoritative; this is UX-only.
const schema = z.object({
  name: z.string().min(1, "Name is required"),
  email: z.string().min(1, "Email is required").email("Enter a valid email address"),
  password: z
    .string()
    .min(15, "Must be at least 15 characters")
    .max(128, "Must be at most 128 characters"),
  business_name: z.string().min(1, "Business name is required"),
  business_timezone: z.string().min(1, "Timezone is required"),
});

type FormValues = z.infer<typeof schema>;

const detectedTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

export function SignupPage() {
  const navigate = useNavigate();
  const { refetchMe, isAuthenticated, isLoading } = useAuth();

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { business_timezone: detectedTimezone },
  });

  const mutation = useMutation({
    mutationFn: authApi.signup,
    onSuccess: async () => {
      await refetchMe();
      navigate("/app/dashboard", { replace: true });
    },
  });

  if (!isLoading && isAuthenticated) {
    return <Navigate to="/app/dashboard" replace />;
  }

  return (
    <div className="mx-auto flex min-h-svh max-w-sm flex-col justify-center gap-6 p-8">
      <h1 className="text-xl font-semibold">Create your account</h1>
      <form
        className="flex flex-col gap-4"
        onSubmit={handleSubmit((values) => mutation.mutate(values))}
      >
        <div className="flex flex-col gap-1">
          <label htmlFor="name" className="text-sm font-medium">
            Your name
          </label>
          <input
            id="name"
            autoComplete="name"
            className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            {...register("name")}
          />
          {errors.name && <p className="text-xs text-destructive">{errors.name.message}</p>}
        </div>
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
        <div className="flex flex-col gap-1">
          <label htmlFor="password" className="text-sm font-medium">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="new-password"
            className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            {...register("password")}
          />
          <p className="text-xs text-muted-foreground">
            At least 15 characters. Passphrases and spaces are fine.
          </p>
          {errors.password && (
            <p className="text-xs text-destructive">{errors.password.message}</p>
          )}
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="business_name" className="text-sm font-medium">
            Business name
          </label>
          <input
            id="business_name"
            className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            {...register("business_name")}
          />
          {errors.business_name && (
            <p className="text-xs text-destructive">{errors.business_name.message}</p>
          )}
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="business_timezone" className="text-sm font-medium">
            Business timezone
          </label>
          <input
            id="business_timezone"
            className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            {...register("business_timezone")}
          />
          {errors.business_timezone && (
            <p className="text-xs text-destructive">{errors.business_timezone.message}</p>
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
          {mutation.isPending ? "Creating account…" : "Create account"}
        </Button>
      </form>
      <Link to="/login" className="text-sm text-muted-foreground underline underline-offset-4">
        Already have an account? Log in
      </Link>
    </div>
  );
}
