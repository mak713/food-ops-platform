// Order List -> Detail pattern (Final Plan §I), mirroring CustomerListPage.tsx's
// search/filter/Table conventions exactly.

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
import { ordersApi, type OrderStatus } from "./api";

type StatusFilter = OrderStatus | "all";

const PAYMENT_STATUS_LABEL: Record<string, string> = {
  UNPAID: "Unpaid",
  PARTIALLY_PAID: "Partially Paid",
  PAID: "Paid",
};

export function OrderListPage() {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const statusParam = status === "all" ? undefined : status;

  const query = useQuery({
    queryKey: ["orders", { q: search, status: statusParam }],
    queryFn: () => ordersApi.list({ q: search || undefined, status: statusParam, limit: 50 }),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Orders</h1>
        <Link to="/app/orders/new" className={buttonVariants({ variant: "default" })}>
          New Order
        </Link>
      </div>

      <div className="flex items-center gap-2">
        <Input
          placeholder="Search by order number…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="max-w-xs"
          aria-label="Search orders"
        />
        <Select value={status} onValueChange={(value) => setStatus(value as StatusFilter)}>
          <SelectTrigger aria-label="Filter by status">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            <SelectItem value="DRAFT">Draft</SelectItem>
            <SelectItem value="CONFIRMED">Confirmed</SelectItem>
            <SelectItem value="READY">Ready</SelectItem>
            <SelectItem value="COMPLETED">Completed</SelectItem>
            <SelectItem value="CANCELED">Canceled</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {query.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {query.isError && (
        <ErrorBanner
          error={query.error}
          onRetry={() => query.refetch()}
          fallbackMessage="Something went wrong loading orders."
        />
      )}

      {query.data && query.data.items.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {search ? "No orders match your search." : "No orders yet."}
        </p>
      )}

      {query.data && query.data.items.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Order #</TableHead>
              <TableHead>Customer</TableHead>
              <TableHead>Fulfillment date</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Total</TableHead>
              <TableHead>Payment</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {query.data.items.map((order) => (
              <TableRow key={order.id}>
                <TableCell>
                  <Link
                    to={`/app/orders/${order.id}`}
                    className="font-medium underline-offset-4 hover:underline"
                  >
                    {order.order_number}
                  </Link>
                </TableCell>
                <TableCell>{order.customer_name ?? "Guest"}</TableCell>
                <TableCell>{order.fulfillment_date ?? "—"}</TableCell>
                <TableCell>
                  <Badge variant={order.status === "DRAFT" ? "secondary" : "default"}>
                    {order.status}
                  </Badge>
                </TableCell>
                <TableCell>${order.final_total}</TableCell>
                <TableCell>{PAYMENT_STATUS_LABEL[order.payment_status]}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  );
}
