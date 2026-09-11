// Shared "API error + Retry" banner for list/detail query failures (Phase 3 plan v3 §15:
// inline error banner with retry, never a blank page).

import { ApiError } from "../../api/client";
import { Button } from "../ui/button";

interface ErrorBannerProps {
  error: unknown;
  onRetry: () => void;
  fallbackMessage?: string;
}

export function ErrorBanner({ error, onRetry, fallbackMessage }: ErrorBannerProps) {
  // Checkpoint 3 manual-testing remediation: an `ApiError` doesn't always carry a
  // parseable `body` — e.g. Vite's dev proxy answers with its own 502 (empty body,
  // not JSON) when the backend is unreachable, which `apiFetch` still wraps as an
  // `ApiError`. Falling through to the generic message whenever `body`'s message is
  // missing (not just when `error` isn't an ApiError at all) avoids rendering a blank
  // banner in that case.
  const message =
    (error instanceof ApiError ? error.body?.error.message : undefined) ??
    fallbackMessage ??
    "Something went wrong. Please try again.";

  return (
    <div
      role="alert"
      className="flex items-center justify-between gap-4 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive"
    >
      <span>{message}</span>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
