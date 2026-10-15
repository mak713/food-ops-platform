// Start Production (RUN-003/004/005/006; Phase 8 Final Architecture Lock §5) —
// batch override with authoritative server-side recomputation, and the RUN-004
// underproduction warning-acknowledgment flow: a rejected Start carrying
// `UNDERPRODUCTION_ACKNOWLEDGMENT_REQUIRED` shows the consequence and a "Start
// anyway" button that echoes back the server-issued `details.fingerprint`
// unmodified — mirroring OrderDetailPage's Confirm-warning pattern exactly. The
// server independently re-verifies the fingerprint under lock on every attempt;
// this UI never substitutes for that check.

import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../../api/client";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { formatQuantityForDisplay } from "../../lib/decimal";
import { productionApi } from "./api";

export function StartProductionPage() {
  const { requirementId } = useParams<{ requirementId: string }>();
  const navigate = useNavigate();
  const [batches, setBatches] = useState<string>("");

  const requirementQuery = useQuery({
    queryKey: ["production-requirements", requirementId],
    queryFn: () => productionApi.getRequirement(requirementId as string),
    retry: false,
  });

  const startMutation = useMutation({
    mutationFn: (acknowledgedFingerprint: string | null) =>
      productionApi.start({
        production_requirement_id: requirementId as string,
        batches: batches ? Number(batches) : null,
        acknowledged_warning_fingerprint: acknowledgedFingerprint,
      }),
    onSuccess: (run) => navigate(`/app/production/runs/${run.id}`),
  });

  const underproductionIssue =
    startMutation.error instanceof ApiError &&
    startMutation.error.body?.error.code === "UNDERPRODUCTION_ACKNOWLEDGMENT_REQUIRED"
      ? startMutation.error.body.error.issues[0]
      : undefined;

  const costBasisIssues =
    startMutation.error instanceof ApiError &&
    startMutation.error.body?.error.code === "PRODUCTION_COST_BASIS_UNKNOWN"
      ? startMutation.error.body.error.issues
      : undefined;

  const genericError =
    startMutation.isError && !underproductionIssue && !costBasisIssues
      ? startMutation.error instanceof ApiError
        ? startMutation.error.body?.error.message
        : "Something went wrong starting production. Please try again."
      : undefined;

  if (requirementQuery.isLoading) {
    return <p className="text-sm text-muted-foreground">Loading…</p>;
  }
  if (requirementQuery.isError) {
    return <ErrorBanner error={requirementQuery.error} onRetry={() => requirementQuery.refetch()} />;
  }
  const requirement = requirementQuery.data;
  if (!requirement) return null;

  return (
    <div className="mx-auto flex max-w-lg flex-col gap-6">
      <h1 className="text-xl font-semibold">Start Production — {requirement.product_name}</h1>

      <dl className="flex flex-col gap-1 rounded-md border p-3 text-sm">
        <div className="flex justify-between">
          <dt className="text-muted-foreground">Demand date</dt>
          <dd>{requirement.demand_date}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-muted-foreground">Demand remaining</dt>
          <dd>{formatQuantityForDisplay(requirement.production_demand_quantity)}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-muted-foreground">Recommended batches</dt>
          <dd>{requirement.recommended_batches ?? "—"}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-muted-foreground">Expected output (recommended)</dt>
          <dd>
            {requirement.expected_output_quantity
              ? formatQuantityForDisplay(requirement.expected_output_quantity)
              : "—"}
          </dd>
        </div>
      </dl>

      <div className="flex flex-col gap-1">
        <label htmlFor="batches" className="text-sm font-medium">
          Batch count (optional override)
        </label>
        <p className="text-xs text-muted-foreground">
          Leave blank to use the recommended batch count above. The server always
          re-computes the outcome fresh, under lock, before starting.
        </p>
        <Input
          id="batches"
          inputMode="numeric"
          placeholder={String(requirement.recommended_batches ?? "")}
          value={batches}
          onChange={(e) => setBatches(e.target.value)}
        />
      </div>

      {costBasisIssues ? (
        <div className="flex flex-col gap-2 rounded-md border border-destructive/50 bg-destructive/10 p-3 text-sm text-destructive">
          <p className="font-semibold">Cannot start — unknown ingredient cost</p>
          <ul className="list-inside list-disc">
            {costBasisIssues.map((issue, index) => (
              <li key={`${issue.code}-${index}`} role="alert">
                {issue.message}
              </li>
            ))}
          </ul>
          <p>Record a replacement or latest-purchase cost for these ingredients, then retry.</p>
        </div>
      ) : underproductionIssue ? (
        <div className="flex flex-col gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
          <p className="font-semibold">This batch count will not fully cover demand</p>
          <p role="alert">{underproductionIssue.message}</p>
          <Button
            type="button"
            size="sm"
            disabled={startMutation.isPending}
            onClick={() =>
              startMutation.mutate(underproductionIssue.details.fingerprint as string)
            }
          >
            Start anyway
          </Button>
        </div>
      ) : (
        <>
          {genericError && (
            <p role="alert" className="text-sm text-destructive">
              {genericError}
            </p>
          )}
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => navigate("/app/production")}
              disabled={startMutation.isPending}
            >
              Back
            </Button>
            <Button
              type="button"
              disabled={startMutation.isPending}
              onClick={() => startMutation.mutate(null)}
            >
              {startMutation.isPending ? "Starting…" : "Start Production"}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
