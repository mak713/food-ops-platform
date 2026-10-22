// Costing & Pricing (Spec SET-003/SET-005): default labor rate and the target contribution
// margin. These are *current* settings — they apply to future work and never rewrite saved
// history — and the copy says so where the owner edits them.
//
// Contribution margin is stored/transferred as a ratio (0.625000) and shown/entered as a
// percentage (62.5); the conversion is a decimal-string shift (ratio.ts), never float math.

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm, useWatch } from "react-hook-form";
import { z } from "zod";
import { CurrencyInput } from "../../components/shared/CurrencyInput";
import { FormField } from "../../components/shared/FormField";
import { Input } from "../../components/ui/input";
import type { BusinessSettings, BusinessSettingsChanges } from "./api";
import { MARGIN_PERCENT_ERROR, percentToRatio, ratioToPercent } from "./ratio";
import { SaveRow } from "./SaveRow";
import { applyServerFieldErrors, useBusinessSettingsSave, useSettingsBase } from "./shared";

const MONEY_PATTERN = /^(\d{1,12})(?:\.(\d{1,2}))?$/;

function normalizeMoney(input: string): string | null {
  const match = MONEY_PATTERN.exec(input.trim());
  if (!match) return null;
  return `${match[1].replace(/^0+(?=\d)/, "")}.${(match[2] ?? "").padEnd(2, "0")}`;
}

const schema = z.object({
  default_labor_rate: z
    .string()
    .refine((v) => normalizeMoney(v) !== null, "Enter an amount such as 18.50 (up to 2 decimals)"),
  target_contribution_margin: z.string().refine((v) => percentToRatio(v).ok, MARGIN_PERCENT_ERROR),
});
type Values = z.infer<typeof schema>;

const FIELDS = ["default_labor_rate", "target_contribution_margin"] as const;

function toValues(s: BusinessSettings): Values {
  return {
    default_labor_rate: s.default_labor_rate,
    target_contribution_margin: ratioToPercent(s.target_contribution_margin),
  };
}

function buildChanges(values: Values, base: BusinessSettings): BusinessSettingsChanges {
  const changes: BusinessSettingsChanges = {};
  const rate = normalizeMoney(values.default_labor_rate);
  if (rate !== null && rate !== base.default_labor_rate) changes.default_labor_rate = rate;
  const margin = percentToRatio(values.target_contribution_margin);
  if (margin.ok && margin.ratio !== base.target_contribution_margin) {
    changes.target_contribution_margin = margin.ratio;
  }
  return changes;
}

interface Props {
  initial: BusinessSettings;
  refresh: () => Promise<BusinessSettings | undefined>;
}

export function CostingPricingForm({ initial, refresh }: Props) {
  const [base, setBase] = useSettingsBase(initial);
  const save = useBusinessSettingsSave(base);

  const {
    register,
    handleSubmit,
    reset,
    setError,
    control,
    formState: { errors },
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: toValues(initial) });

  const values = useWatch({ control }) as Values;
  const changes = buildChanges(values, base);
  // An invalid entry keeps Save enabled so submitting surfaces the validation message instead
  // of leaving the user with a silently disabled button.
  const hasChanges = Object.keys(changes).length > 0 || !schema.safeParse(values).success;

  const submit = handleSubmit((values) => {
    const toSave = buildChanges(values, base);
    if (Object.keys(toSave).length === 0) return;
    save.mutate(toSave, {
      onSuccess: (saved) => {
        setBase(saved);
        reset(toValues(saved));
      },
      onError: (error) => applyServerFieldErrors(error, setError, FIELDS),
    });
  });

  const handleRefreshStale = async () => {
    save.reset();
    const fresh = await refresh();
    if (fresh) {
      setBase(fresh);
      reset(toValues(fresh));
    }
  };

  return (
    <form className="flex max-w-lg flex-col gap-4" onSubmit={submit} noValidate>
      <FormField
        id="default_labor_rate"
        label="Default labor rate (per hour)"
        error={errors.default_labor_rate?.message}
      >
        <CurrencyInput
          id="default_labor_rate"
          aria-describedby="labor-rate-help"
          {...register("default_labor_rate")}
        />
        <p id="labor-rate-help" className="text-xs text-muted-foreground">
          Applies to future production starts and subsequent recalculations. Existing production
          runs, completed costs, and already-calculated planned estimates are not rewritten.
        </p>
      </FormField>

      <FormField
        id="target_contribution_margin"
        label="Target contribution margin (%)"
        error={errors.target_contribution_margin?.message}
      >
        <div className="relative flex items-center">
          <Input
            id="target_contribution_margin"
            type="text"
            inputMode="decimal"
            className="pr-7"
            aria-describedby="margin-help"
            {...register("target_contribution_margin")}
          />
          <span className="pointer-events-none absolute right-2.5 text-sm text-muted-foreground">
            %
          </span>
        </div>
        <p id="margin-help" className="text-xs text-muted-foreground">
          Optional. For example 62.5 for 62.5%. Leave blank for no target. It is a goal for
          reference; it does not change any saved price or cost.
        </p>
      </FormField>

      <SaveRow
        pending={save.isPending}
        hasChanges={hasChanges}
        saved={save.isSuccess}
        error={save.error}
        onRefreshStale={handleRefreshStale}
      />
    </form>
  );
}
