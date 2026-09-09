import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { Button } from "../../components/ui/button";
import { authApi } from "./api";
import { useAuth } from "./AuthContext";

const schema = z.object({
  email: z.string().min(1, "Email is required"),
  password: z.string().min(1, "Password is required"),
});

type FormValues = z.infer<typeof schema>;

const DEFAULT_AUTHENTICATED_PATH = "/app/dashboard";

// Only ever returns a destination inside the app's own protected area. ProtectedRoute
// (see ProtectedRoute.tsx) only ever generates `next` from a real /app/* location, so
// anything outside that exact shape — an absolute/external URL, a protocol-relative
// "//host" value, or anything else unexpected — is rejected in favor of the default
// rather than followed blindly (Checkpoint 2 review: no open redirect via `next`).
export function resolveNextPath(rawNext: string | null): string {
  if (rawNext && /^\/app(\/.*)?$/.test(rawNext)) {
    return rawNext;
  }
  return DEFAULT_AUTHENTICATED_PATH;
}

export function LoginPage() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const { refetchMe, isAuthenticated, isLoading } = useAuth();
  const next = resolveNextPath(searchParams.get("next"));

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>({ resolver: zodResolver(schema) });

  const mutation = useMutation({
    mutationFn: authApi.login,
    onSuccess: async () => {
      await refetchMe();
      navigate(next, { replace: true });
    },
  });

  // Uses the same resolved `next` as the post-login navigate() above. Previously this
  // redirected unconditionally to /app/dashboard — since isAuthenticated can flip true
  // (via the awaited refetchMe() above) while LoginPage is still mounted, this check
  // could win the race against the mutation's own next-aware navigate() and silently
  // discard the intended destination (Checkpoint 2 review, login return-path bug).
  // Unifying the target eliminates the race instead of trying to out-time it.
  if (!isLoading && isAuthenticated) {
    return <Navigate to={next} replace />;
  }

  return (
    <div className="mx-auto flex min-h-svh max-w-sm flex-col justify-center gap-6 p-8">
      <h1 className="text-xl font-semibold">Log in</h1>
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
        <div className="flex flex-col gap-1">
          <label htmlFor="password" className="text-sm font-medium">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            className="rounded-md border border-input bg-background px-3 py-2 text-sm"
            {...register("password")}
          />
          {errors.password && (
            <p className="text-xs text-destructive">{errors.password.message}</p>
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
          {mutation.isPending ? "Logging in…" : "Log in"}
        </Button>
      </form>
      <div className="flex flex-col gap-1 text-sm text-muted-foreground">
        <Link to="/signup" className="underline underline-offset-4">
          Create an account
        </Link>
        <Link to="/password-reset" className="underline underline-offset-4">
          Forgot your password?
        </Link>
      </div>
    </div>
  );
}
