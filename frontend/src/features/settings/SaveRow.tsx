import { ApiError } from "../../api/client";
import { StaleVersionPanel } from "../../components/shared/StaleVersionPanel";
import { Button } from "../../components/ui/button";
import { errorMessage, isStaleVersion } from "./shared";

interface SaveRowProps {
  pending: boolean;
  hasChanges: boolean;
  saved: boolean;
  error: unknown;
  onRefreshStale: () => void;
}

export function SaveRow({ pending, hasChanges, saved, error, onRefreshStale }: SaveRowProps) {
  const stale = isStaleVersion(error);
  return (
    <div className="flex flex-col gap-3">
      {stale && <StaleVersionPanel onRefresh={onRefreshStale} />}
      {Boolean(error) && !stale && !(error instanceof ApiError && error.status === 422) && (
        <p role="alert" className="text-sm text-destructive">
          {errorMessage(error)}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={pending || !hasChanges}>
          {pending ? "Saving…" : "Save changes"}
        </Button>
        {saved && !hasChanges && (
          <p role="status" className="text-sm text-muted-foreground">
            Saved
          </p>
        )}
      </div>
    </div>
  );
}
