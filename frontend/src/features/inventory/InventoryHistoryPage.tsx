// Inventory History hub (Phase 5 Plan §F) — a single module-level entry point covering
// both Ingredient and Purchased Product Inventory history via two tabs, each with its own
// resource picker (populated from every resource regardless of active state — approval
// decision 8) and an embedded shared history table. Two separate, typed queries against
// the two existing ledger tables — never a merged/polymorphic query, and no schema
// redesign, per the explicit instruction not to invent an unnecessary abstraction here.

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { InventoryTransactionTable } from "../../components/shared/InventoryTransactionTable";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import { ingredientsApi } from "../ingredients/api";
import { ingredientInventoryApi } from "../ingredients/inventoryApi";
import { purchasedInventoryApi } from "./purchasedInventoryApi";

type Tab = "ingredients" | "purchased";

export function InventoryHistoryPage() {
  const [tab, setTab] = useState<Tab>("ingredients");
  const [selectedIngredientId, setSelectedIngredientId] = useState<string>("");
  const [selectedProductId, setSelectedProductId] = useState<string>("");

  const ingredientsQuery = useQuery({
    queryKey: ["ingredients", "all"],
    queryFn: () => ingredientsApi.listAll(),
    enabled: tab === "ingredients",
  });
  const purchasedQuery = useQuery({
    queryKey: ["purchased-inventory", "all"],
    queryFn: () => purchasedInventoryApi.listAll(),
    enabled: tab === "purchased",
  });

  const selectedIngredient = ingredientsQuery.data?.find((i) => i.id === selectedIngredientId);
  const selectedProduct = purchasedQuery.data?.find((p) => p.product_id === selectedProductId);

  return (
    <div className="flex max-w-3xl flex-col gap-4">
      <h1 className="text-xl font-semibold">Inventory History</h1>

      <div className="flex gap-2">
        <Button
          type="button"
          variant={tab === "ingredients" ? "default" : "outline"}
          size="sm"
          onClick={() => setTab("ingredients")}
        >
          Ingredients
        </Button>
        <Button
          type="button"
          variant={tab === "purchased" ? "default" : "outline"}
          size="sm"
          onClick={() => setTab("purchased")}
        >
          Purchased Products
        </Button>
      </div>

      {tab === "ingredients" ? (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <Select
              value={selectedIngredientId}
              onValueChange={(value) => setSelectedIngredientId(value ?? "")}
            >
              <SelectTrigger aria-label="Choose an ingredient" className="w-64">
                <SelectValue placeholder="Choose an ingredient…">
                  {(value: string) =>
                    value
                      ? (ingredientsQuery.data?.find((i) => i.id === value)?.name ?? value)
                      : "Choose an ingredient…"
                  }
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {(ingredientsQuery.data ?? []).map((ingredient) => (
                  <SelectItem key={ingredient.id} value={ingredient.id}>
                    {ingredient.name}
                    {!ingredient.is_active ? " (Archived)" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {selectedIngredient && !selectedIngredient.is_active && (
              <Badge variant="secondary">Archived</Badge>
            )}
          </div>

          {selectedIngredientId && (
            <InventoryTransactionTable
              queryKey={["ingredients", selectedIngredientId, "inventory", "transactions"]}
              fetchPage={(params) =>
                ingredientInventoryApi.listTransactions(selectedIngredientId, params)
              }
            />
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <Select
              value={selectedProductId}
              onValueChange={(value) => setSelectedProductId(value ?? "")}
            >
              <SelectTrigger aria-label="Choose a purchased product" className="w-64">
                <SelectValue placeholder="Choose a purchased product…">
                  {(value: string) =>
                    value
                      ? (purchasedQuery.data?.find((p) => p.product_id === value)?.product_name ??
                        value)
                      : "Choose a purchased product…"
                  }
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {(purchasedQuery.data ?? []).map((product) => (
                  <SelectItem key={product.product_id} value={product.product_id}>
                    {product.product_name}
                    {!product.product_is_active ? " (Inactive)" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {selectedProduct && !selectedProduct.product_is_active && (
              <Badge variant="secondary">Inactive</Badge>
            )}
          </div>

          {selectedProductId && (
            <InventoryTransactionTable
              queryKey={["products", selectedProductId, "purchased-inventory", "transactions"]}
              fetchPage={(params) =>
                purchasedInventoryApi.listTransactions(selectedProductId, params)
              }
            />
          )}
        </div>
      )}
    </div>
  );
}
