// Inventory-module landing page (Phase 5 Plan §F) — the top-nav "Inventory" link now
// points here instead of hard-linking straight to the Ingredients list, so Purchased
// Product Inventory has a proper module-level presence rather than being reachable only
// through Product-detail nesting.

import { Link } from "react-router-dom";

function HubCard({
  to,
  title,
  description,
}: {
  to: string;
  title: string;
  description: string;
}) {
  return (
    <Link
      to={to}
      className="flex flex-col gap-1 rounded-md border border-border p-4 hover:border-foreground/50"
    >
      <span className="text-lg font-medium">{title}</span>
      <span className="text-sm text-muted-foreground">{description}</span>
    </Link>
  );
}

export function InventoryHubPage() {
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Inventory</h1>
      <div className="grid max-w-2xl grid-cols-1 gap-4 sm:grid-cols-2">
        <HubCard
          to="/app/inventory/ingredients"
          title="Ingredients"
          description="Current balances, restock, adjustments, and cost basis for every ingredient."
        />
        <HubCard
          to="/app/inventory/purchased-products"
          title="Purchased Products"
          description="Current balances, restock, and adjustments for resold products."
        />
        <HubCard
          to="/app/inventory/history"
          title="Inventory History"
          description="Browse the transaction history for any ingredient or purchased product."
        />
      </div>
    </div>
  );
}
