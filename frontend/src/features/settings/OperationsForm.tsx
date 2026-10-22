// Operations (Spec SET-003/SET-004): the fulfillment warning window and the default Shopping
// List horizon. Both are live inputs to the Dashboard / Shopping List, so a change is
// reflected there on the next read.
//
// Warning minutes must be > 0 (the Spec's only rule). No product maximum is invented: the
// only upper bound is what the database column can store (PostgreSQL INTEGER), and the
// Dashboard comparison is overflow-safe for every storable value.

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm, useWatch } from "react-hook-form";
import { z } from "zod";
import { FormField } from "../../components/shared/FormField";
import { Input } from "../../components/ui/input";
import type { BusinessSettings, BusinessSettingsChanges } from "./api";
import { SaveRow } from "./SaveRow";
import {
  applyServerFieldErrors,
  nativeSelectClass,
  useBusinessSettingsSave,
  useSettingsBase,
} from "./shared";

const POSTGRES_INTEGER_MAX = 2_147_483_647;
const MINUTES_ERROR = `Enter a whole number of minutes from 1 to ${POSTGRES_INTEGER_MAX.toLocaleString("en-US")}.`;

function parseMinutes(input: string): number | null {
  const trimmed = input.trim();
  if (!/^\d{1,10}$/.test(trimmed)) return null;
  const value = Number(trimmed); // integers this short are exact in a double
  return value >= 1 && value <= POSTGRES_INTEGER_MAX ? value : null;
}

const schema = z.object({
  fulfillment_warning_minutes: z.string().refine((v) => parseMinutes(v) !== null, MINUTES_ERROR),
  shopping_horizon_days: z.enum(["3", "7"]),
});
type Values = z.infer<typeof schema>;

const FIELDS = ["fulfillment_warning_minutes", "shopping_horizon_days"] as const;

function toValues(s: BusinessSettings): Values {
  return {
    fulfillment_warning_minutes: String(s.fulfillment_warning_minutes),
    shopping_horizon_days: s.shopping_horizon_days === 3 ? "3" : "7",
  };
}

function buildChanges(values: Values, base: BusinessSettings): BusinessSettingsChanges {
  const changes: BusinessSettingsChanges = {};
  const minutes = parseMinutes(values.fulfillment_warning_minutes);
  if (minutes !== null && minutes !== base.fulfillment_warning_minutes) {
    changes.fulfillment_warning_minutes = minutes;
  }
  const horizon = Number(values.shopping_horizon_days);
  if (horizon !== base.shopping_horizon_days) changes.shopping_horizon_days = horizon;
  return changes;
}

interface Props {
  initial: BusinessSettings;
  refresh: () => Promise<BusinessSettings | undefined>;
}

export function OperationsForm({ initial, refresh }: Props) {
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
        id="fulfillment_warning_minutes"
        label="Fulfillment warning window (minutes)"
        error={errors.fulfillment_warning_minutes?.message}
      >
        <Input
          id="fulfillment_warning_minutes"
          type="text"
          inputMode="numeric"
          aria-describedby="warning-help"
          {...register("fulfillment_warning_minutes")}
        />
        <p id="warning-help" className="text-xs text-muted-foreground">
          The Dashboard flags an order that is not yet Ready when its fulfillment time is within
          this many minutes.
        </p>
      </FormField>

      <FormField
        id="shopping_horizon_days"
        label="Default Shopping List horizon"
        error={errors.shopping_horizon_days?.message}
      >
        <select
          id="shopping_horizon_days"
          className={nativeSelectClass}
          aria-describedby="horizon-help"
          {...register("shopping_horizon_days")}
        >
          <option value="3">3 days</option>
          <option value="7">7 days</option>
        </select>
        <p id="horizon-help" className="text-xs text-muted-foreground">
          How far ahead the Shopping List looks by default. You can still pick 3 or 7 days (or a
          custom range) on the Shopping List itself.
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
