// List → Detail pattern (Spec §5.3), mirroring CustomerListPage/ProductListPage exactly.
// Lives under Inventory (Spec §5; Phase 4 Plan v4 §13), not a top-level nav item.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Badge } from "../../components/ui/badge";
import { buttonVariants } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../../components/ui/table";
import { ingredientsApi } from "./api";

type ActiveFilter = "active" | "archived" | "all";

export function IngredientListPage() {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<ActiveFilter>("active");
  const isActiveParam = filter === "all" ? undefined : filter === "active";

  const query = useQuery({
    queryKey: ["ingredients", { q: search, is_active: isActiveParam }],
    queryFn: () =>
      ingredientsApi.list({ q: search || undefined, is_active: isActiveParam, limit: 50 }),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Ingredients</h1>
        <Link to="/app/inventory/ingredients/new" className={buttonVariants({ variant: "default" })}>
          New Ingredient
        </Link>
      </div>

      <div className="flex items-center gap-2">
        <Input
          placeholder="Search by name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-xs"
          aria-label="Search ingredients"
        />
        <Select value={filter} onValueChange={(value) => setFilter(value as ActiveFilter)}>
          <SelectTrigger aria-label="Filter by status">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="active">Active</SelectItem>
            <SelectItem value="archived">Archived</SelectItem>
            <SelectItem value="all">All</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {query.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {query.isError && (
        <ErrorBanner
          error={query.error}
          onRetry={() => query.refetch()}
          fallbackMessage="Something went wrong loading ingredients."
        />
      )}

      {query.data && query.data.items.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {search
            ? "No ingredients match your search."
            : filter === "archived"
              ? "No archived ingredients."
              : "No ingredients yet."}
        </p>
      )}

      {query.data && query.data.items.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Family</TableHead>
              <TableHead>Canonical unit</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data.items.map((ingredient) => (
              <TableRow key={ingredient.id}>
                <TableCell>
                  <Link
                    to={`/app/inventory/ingredients/${ingredient.id}`}
                    className="font-medium underline-offset-4 hover:underline"
                  >
                    {ingredient.name}
                  </Link>
                </TableCell>
                <TableCell>{ingredient.measurement_family}</TableCell>
                <TableCell>{ingredient.canonical_unit}</TableCell>
                <TableCell>
                  <Badge variant={ingredient.is_active ? "default" : "secondary"}>
                    {ingredient.is_active ? "Active" : "Archived"}
                  </Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
