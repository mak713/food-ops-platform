// List → Detail pattern (Spec §5.3). Active/Archived/All filter (Phase 3 plan v3 §15).

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
import { productsApi } from "./api";

type ActiveFilter = "active" | "archived" | "all";

export function ProductListPage() {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<ActiveFilter>("active");
  const isActiveParam = filter === "all" ? undefined : filter === "active";

  const query = useQuery({
    queryKey: ["products", { q: search, is_active: isActiveParam }],
    queryFn: () =>
      productsApi.list({ q: search || undefined, is_active: isActiveParam, limit: 50 }),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Products</h1>
        <Link to="/app/products/new" className={buttonVariants({ variant: "default" })}>
          New Product
        </Link>
      </div>

      <div className="flex items-center gap-2">
        <Input
          placeholder="Search by name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-xs"
          aria-label="Search products"
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
          fallbackMessage="Something went wrong loading products."
        />
      )}

      {query.data && query.data.items.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {search
            ? "No products match your search."
            : filter === "archived"
              ? "No archived products."
              : "No products yet."}
        </p>
      )}

      {query.data && query.data.items.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Type</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data.items.map((product) => (
              <TableRow key={product.id}>
                <TableCell>
                  <Link
                    to={`/app/products/${product.id}`}
                    className="font-medium underline-offset-4 hover:underline"
                  >
                    {product.name}
                  </Link>
                </TableCell>
                <TableCell>
                  {product.product_type === "PRODUCED" ? "Produced" : "Purchased"}
                </TableCell>
                <TableCell>
                  <Badge variant={product.is_active ? "default" : "secondary"}>
                    {product.is_active ? "Active" : "Archived"}
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
