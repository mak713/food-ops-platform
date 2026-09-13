// Current Purchased Product Inventory — the top-level Inventory-module list screen
// (Phase 5 Plan §F), reachable directly from /app/inventory rather than only via a
// Product's own detail page. Mirrors IngredientListPage's active/archived/all filter.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Badge } from "../../components/ui/badge";
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
import { formatDecimal } from "../../lib/decimal";
import { purchasedInventoryApi } from "./purchasedInventoryApi";

type ActiveFilter = "active" | "inactive" | "all";

export function PurchasedProductInventoryListPage() {
  const [filter, setFilter] = useState<ActiveFilter>("active");
  const isActiveParam = filter === "all" ? undefined : filter === "active";

  const query = useQuery({
    queryKey: ["purchased-inventory", { is_active: isActiveParam }],
    queryFn: () => purchasedInventoryApi.listForBusiness({ is_active: isActiveParam, limit: 50 }),
  });

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Purchased Product Inventory</h1>

      <Select value={filter} onValueChange={(value) => setFilter(value as ActiveFilter)}>
        <SelectTrigger aria-label="Filter by status" className="w-40">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="active">Active</SelectItem>
          <SelectItem value="inactive">Inactive</SelectItem>
          <SelectItem value="all">All</SelectItem>
        </SelectContent>
      </Select>

      {query.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {query.isError && (
        <ErrorBanner
          error={query.error}
          onRetry={() => query.refetch()}
          fallbackMessage="Something went wrong loading purchased inventory."
        />
      )}

      {query.data && query.data.items.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {filter === "inactive" ? "No inactive purchased products." : "No purchased products yet."}
        </p>
      )}

      {query.data && query.data.items.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Product</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Physical quantity</TableHead>
              <TableHead>Weighted-average cost</TableHead>
              <TableHead>Effective replacement cost</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data.items.map((item) => (
              <TableRow key={item.product_id}>
                <TableCell>
                  <Link
                    to={`/app/products/${item.product_id}`}
                    className="font-medium underline-offset-4 hover:underline"
                  >
                    {item.product_name}
                  </Link>
                </TableCell>
                <TableCell>
                  <Badge variant={item.product_is_active ? "default" : "secondary"}>
                    {item.product_is_active ? "Active" : "Inactive"}
                  </Badge>
                </TableCell>
                <TableCell>
                  {item.physical_quantity != null ? (
                    formatDecimal(item.physical_quantity)
                  ) : (
                    <span className="text-muted-foreground">Not initialized</span>
                  )}
                </TableCell>
                <TableCell>
                  {item.weighted_average_unit_cost != null
                    ? formatDecimal(item.weighted_average_unit_cost)
                    : "—"}
                </TableCell>
                <TableCell>
                  {item.effective_replacement_cost != null
                    ? formatDecimal(item.effective_replacement_cost)
                    : "—"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
