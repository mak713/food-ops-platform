// Shared revision-content editor used by both RecipeCreatePage and RecipeEditPage (Phase
// 4 Plan v4 §13) — yield/active-time/elapsed-time/notes plus a dynamic ingredient-lines
// editor. The "add ingredient" selector only offers active Ingredients (Phase 4 Plan v4
// §5.5/§13); a line already in the draft whose Ingredient is archived (inherited from the
// base revision being edited) remains visible/editable and is labeled "Archived" rather
// than being force-removed. Per-line unit choices are filtered to that line's Ingredient's
// measurement family via UNIT_OPTIONS_BY_FAMILY, mirroring the backend's own validation so
// an incompatible unit can never even be selected.

import { zodResolver } from "@hookform/resolvers/zod";
import type { ReactNode } from "react";
import { Controller, useFieldArray, useForm } from "react-hook-form";
import { z } from "zod";
import { ApiError } from "../../api/client";
import { Badge } from "../../components/ui/badge";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../components/ui/select";
import { Textarea } from "../../components/ui/textarea";
import { FAMILY_BY_UNIT, UNIT_OPTIONS_BY_FAMILY } from "../../lib/units";
import type { MeasurementFamily } from "../ingredients/api";
import type { RecipeContentInput } from "./recipeApi";

export interface IngredientOption {
  id: string;
  name: string;
  // Omitted for a carried-forward archived Ingredient (its family isn't part of the
  // RecipeRevisionIngredientResponse shape) — the form falls back to deriving the family
  // from the line's own already-set unit via FAMILY_BY_UNIT in that case.
  measurement_family?: MeasurementFamily;
  is_active: boolean;
}

const lineSchema = z.object({
  ingredient_id: z.string().min(1, "Ingredient is required"),
  quantity: z
    .string()
    .min(1, "Quantity is required")
    .refine((v) => !Number.isNaN(Number(v)) && Number(v) > 0, "Quantity must be greater than 0"),
  unit: z.string().min(1, "Unit is required"),
});

const schema = z
  .object({
    yield_quantity: z
      .string()
      .min(1, "Yield is required")
      .refine((v) => !Number.isNaN(Number(v)) && Number(v) > 0, "Yield must be greater than 0"),
    active_time_minutes: z
      .string()
      .min(1, "Active time is required")
      .refine(
        (v) => Number.isInteger(Number(v)) && Number(v) >= 0,
        "Active time must be a whole number of minutes, 0 or greater",
      ),
    elapsed_time_minutes: z
      .string()
      .optional()
      .refine(
        (v) => !v || (Number.isInteger(Number(v)) && Number(v) >= 0),
        "Elapsed time must be a whole number of minutes, 0 or greater",
      ),
    notes: z.string().optional(),
    ingredients: z.array(lineSchema).min(1, "At least one ingredient line is required"),
  })
  .refine(
    (data) => !data.elapsed_time_minutes || Number(data.elapsed_time_minutes) >= Number(data.active_time_minutes),
    { message: "Elapsed time must be at least the active time", path: ["elapsed_time_minutes"] },
  )
  .refine(
    (data) => {
      const ids = data.ingredients.map((line) => line.ingredient_id).filter(Boolean);
      return new Set(ids).size === ids.length;
    },
    { message: "The same ingredient cannot be used twice in one recipe", path: ["ingredients"] },
  );

export type RecipeContentFormValues = z.infer<typeof schema>;

interface RecipeContentFormProps {
  defaultValues: RecipeContentFormValues;
  activeIngredients: IngredientOption[];
  ingredientLookup: Record<string, IngredientOption>;
  onSubmit: (values: RecipeContentInput) => void;
  isPending: boolean;
  mutationError: unknown;
  submitLabel: string;
  headerNote?: ReactNode;
  /** When provided, renders a Cancel button beside the submit button — navigates away
   * without submitting anything or creating a Recipe Revision (Phase 4 remediation §4). */
  onCancel?: () => void;
}

export function RecipeContentForm({
  defaultValues,
  activeIngredients,
  ingredientLookup,
  onSubmit,
  isPending,
  mutationError,
  submitLabel,
  headerNote,
  onCancel,
}: RecipeContentFormProps) {
  const {
    register,
    control,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
  } = useForm<RecipeContentFormValues>({ resolver: zodResolver(schema), defaultValues });

  const { fields, append, remove } = useFieldArray({ control, name: "ingredients" });
  const watchedLines = watch("ingredients");

  const submit = handleSubmit((values) => {
    onSubmit({
      yield_quantity: values.yield_quantity,
      active_time_minutes: Number(values.active_time_minutes),
      elapsed_time_minutes: values.elapsed_time_minutes ? Number(values.elapsed_time_minutes) : null,
      notes: values.notes || null,
      ingredients: values.ingredients.map((line) => ({
        ingredient_id: line.ingredient_id,
        quantity: line.quantity,
        unit: line.unit,
      })),
    });
  });

  return (
    <form className="flex flex-col gap-4" onSubmit={submit}>
      {headerNote}

      <div className="flex flex-col gap-1">
        <label htmlFor="yield_quantity" className="text-sm font-medium">
          Yield
        </label>
        <Input id="yield_quantity" inputMode="decimal" {...register("yield_quantity")} />
        {errors.yield_quantity && (
          <p className="text-xs text-destructive">{errors.yield_quantity.message}</p>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="active_time_minutes" className="text-sm font-medium">
          Active time (minutes)
        </label>
        <Input id="active_time_minutes" inputMode="numeric" {...register("active_time_minutes")} />
        {errors.active_time_minutes && (
          <p className="text-xs text-destructive">{errors.active_time_minutes.message}</p>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="elapsed_time_minutes" className="text-sm font-medium">
          Elapsed time (minutes, optional)
        </label>
        <Input
          id="elapsed_time_minutes"
          inputMode="numeric"
          {...register("elapsed_time_minutes")}
        />
        {errors.elapsed_time_minutes && (
          <p className="text-xs text-destructive">{errors.elapsed_time_minutes.message}</p>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="notes" className="text-sm font-medium">
          Notes (optional)
        </label>
        <Textarea id="notes" {...register("notes")} />
      </div>

      <div className="flex flex-col gap-2">
        <span className="text-sm font-medium">Ingredients</span>
        {errors.ingredients?.root?.message && (
          <p role="alert" className="text-xs text-destructive">
            {errors.ingredients.root.message}
          </p>
        )}
        {typeof errors.ingredients?.message === "string" && (
          <p role="alert" className="text-xs text-destructive">
            {errors.ingredients.message}
          </p>
        )}

        {fields.map((field, index) => {
          const currentId = watchedLines?.[index]?.ingredient_id ?? field.ingredient_id;
          const currentUnit = watchedLines?.[index]?.unit ?? field.unit;
          const info = ingredientLookup[currentId];
          const family = info?.measurement_family ?? FAMILY_BY_UNIT[currentUnit];
          const unitOptions = family ? UNIT_OPTIONS_BY_FAMILY[family] : [];
          return (
            <div key={field.id} className="flex flex-wrap items-end gap-2 rounded-md border border-border p-2">
              <div className="flex flex-col gap-1">
                <label
                  htmlFor={`ingredients.${index}.ingredient_id`}
                  className="text-xs text-muted-foreground"
                >
                  Ingredient
                </label>
                {info && !info.is_active ? (
                  <div className="flex w-40 items-center gap-1 text-sm">
                    <span>{info.name}</span>
                    <Badge variant="secondary">Archived</Badge>
                  </div>
                ) : (
                  <Controller
                    name={`ingredients.${index}.ingredient_id`}
                    control={control}
                    render={({ field: controllerField }) => (
                      <Select
                        value={controllerField.value}
                        onValueChange={(value) => {
                          controllerField.onChange(value);
                          // The new Ingredient may belong to a different measurement
                          // family — a unit valid for the old one could be meaningless
                          // (or outright rejected server-side) for the new one, so the
                          // line's unit is cleared and must be explicitly re-chosen
                          // rather than silently carried over.
                          setValue(`ingredients.${index}.unit`, "", { shouldDirty: true });
                        }}
                      >
                        <SelectTrigger id={`ingredients.${index}.ingredient_id`} className="w-40">
                          <SelectValue placeholder="Choose…">
                            {(value: string) =>
                              value ? (ingredientLookup[value]?.name ?? value) : "Choose…"
                            }
                          </SelectValue>
                        </SelectTrigger>
                        <SelectContent>
                          {activeIngredients.map((option) => (
                            <SelectItem key={option.id} value={option.id}>
                              {option.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                )}
                {errors.ingredients?.[index]?.ingredient_id && (
                  <p className="text-xs text-destructive">
                    {errors.ingredients[index]?.ingredient_id?.message}
                  </p>
                )}
              </div>

              <div className="flex flex-col gap-1">
                <label
                  htmlFor={`ingredients.${index}.quantity`}
                  className="text-xs text-muted-foreground"
                >
                  Quantity
                </label>
                <Input
                  id={`ingredients.${index}.quantity`}
                  className="w-24"
                  inputMode="decimal"
                  {...register(`ingredients.${index}.quantity` as const)}
                />
                {errors.ingredients?.[index]?.quantity && (
                  <p className="text-xs text-destructive">
                    {errors.ingredients[index]?.quantity?.message}
                  </p>
                )}
              </div>

              <div className="flex flex-col gap-1">
                <label
                  htmlFor={`ingredients.${index}.unit`}
                  className="text-xs text-muted-foreground"
                >
                  Unit
                </label>
                <Controller
                  name={`ingredients.${index}.unit`}
                  control={control}
                  render={({ field: controllerField }) => (
                    <Select value={controllerField.value} onValueChange={controllerField.onChange}>
                      <SelectTrigger id={`ingredients.${index}.unit`} className="w-32">
                        <SelectValue placeholder="Unit" />
                      </SelectTrigger>
                      <SelectContent>
                        {unitOptions.map((unit) => (
                          <SelectItem key={unit.value} value={unit.value}>
                            {unit.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                />
                {errors.ingredients?.[index]?.unit && (
                  <p className="text-xs text-destructive">
                    {errors.ingredients[index]?.unit?.message}
                  </p>
                )}
              </div>

              <Button type="button" variant="outline" size="sm" onClick={() => remove(index)}>
                Remove
              </Button>
            </div>
          );
        })}

        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={activeIngredients.length === 0}
          onClick={() =>
            append({
              ingredient_id: activeIngredients[0]?.id ?? "",
              quantity: "",
              unit: "",
            })
          }
        >
          Add ingredient
        </Button>
      </div>

      {mutationError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationError instanceof ApiError
            ? mutationError.body?.error.message
            : "Something went wrong. Please try again."}
        </p>
      ) : null}

      <div className="flex gap-2">
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel} disabled={isPending}>
            Cancel
          </Button>
        )}
        <Button type="submit" disabled={isPending}>
          {isPending ? "Saving…" : submitLabel}
        </Button>
      </div>
    </form>
  );
}
