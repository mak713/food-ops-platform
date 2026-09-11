// List → Detail pattern (Spec §5.3): search/filter, then results. Active/Archived/All
// filter maps directly to the `is_active` query param (Phase 3 plan v3 §15).

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { buttonVariants } from "../../components/ui/button";
import { ErrorBanner } from "../../components/shared/ErrorBanner";
import { Badge } from "../../components/ui/badge";
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
import { customersApi } from "./api";

type ActiveFilter = "active" | "archived" | "all";

export function CustomerListPage() {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState<ActiveFilter>("active");
  const isActiveParam = filter === "all" ? undefined : filter === "active";

  const query = useQuery({
    queryKey: ["customers", { q: search, is_active: isActiveParam }],
    queryFn: () =>
      customersApi.list({ q: search || undefined, is_active: isActiveParam, limit: 50 }),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Customers</h1>
        <Link to="/app/customers/new" className={buttonVariants({ variant: "default" })}>
          New Customer
        </Link>
      </div>

      <div className="flex items-center gap-2">
        <Input
          placeholder="Search by name…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-xs"
          aria-label="Search customers"
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
          fallbackMessage="Something went wrong loading customers."
        />
      )}

      {query.data && query.data.items.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {search
            ? "No customers match your search."
            : filter === "archived"
              ? "No archived customers."
              : "No customers yet."}
        </p>
      )}

      {query.data && query.data.items.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Phone</TableHead>
              <TableHead>Email</TableHead>
              <TableHead>Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data.items.map((customer) => (
              <TableRow key={customer.id}>
                <TableCell>
                  <Link
                    to={`/app/customers/${customer.id}`}
                    className="font-medium underline-offset-4 hover:underline"
                  >
                    {customer.name}
                  </Link>
                </TableCell>
                <TableCell>{customer.phone ?? "—"}</TableCell>
                <TableCell>{customer.email ?? "—"}</TableCell>
                <TableCell>
                  <Badge variant={customer.is_active ? "default" : "secondary"}>
                    {customer.is_active ? "Active" : "Archived"}
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
