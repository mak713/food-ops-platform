// Business Profile (Spec AUTH-007, SET-001/002/003): name, business type, contact details,
// address and the Business timezone. The timezone is the one setting whose change can alter
// how existing data is *interpreted* (never what is stored), so a changed timezone is only
// saved after an explicit confirmation.
//
// The timezone control is an editable text input. The datalist offers the browser's known
// IANA names as suggestions only — it is never the validity gate; the server validates every
// value (including legacy aliases such as US/Pacific that a browser list may omit).

import { zodResolver } from "@hookform/resolvers/zod";
import { useMemo, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { z } from "zod";
import { FormField } from "../../components/shared/FormField";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../../components/ui/alert-dialog";
import { Input } from "../../components/ui/input";
import { Textarea } from "../../components/ui/textarea";
import type { BusinessSettings, BusinessSettingsChanges } from "./api";
import { SaveRow } from "./SaveRow";
import { applyServerFieldErrors, useBusinessSettingsSave, useSettingsBase } from "./shared";

const EMAIL_SHAPE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

const schema = z.object({
  name: z.string().trim().min(1, "Business name is required"),
  business_type: z.string(),
  contact_phone: z.string(),
  contact_email: z
    .string()
    .refine((v) => v.trim() === "" || EMAIL_SHAPE.test(v.trim()), "Enter a valid email address"),
  address_text: z.string(),
  timezone: z.string().trim().min(1, "Timezone is required"),
});
type Values = z.infer<typeof schema>;

const FIELDS = [
  "name",
  "business_type",
  "contact_phone",
  "contact_email",
  "address_text",
  "timezone",
] as const;

function toValues(s: BusinessSettings): Values {
  return {
    name: s.name,
    business_type: s.business_type ?? "",
    contact_phone: s.contact_phone ?? "",
    contact_email: s.contact_email ?? "",
    address_text: s.address_text ?? "",
    timezone: s.timezone,
  };
}

function buildChanges(values: Values, base: BusinessSettings): BusinessSettingsChanges {
  const changes: BusinessSettingsChanges = {};
  const name = values.name.trim();
  if (name !== base.name) changes.name = name;
  const timezone = values.timezone.trim();
  if (timezone !== base.timezone) changes.timezone = timezone;
  for (const key of ["business_type", "contact_phone", "contact_email", "address_text"] as const) {
    const next = values[key].trim() || null;
    if (next !== base[key]) changes[key] = next;
  }
  return changes;
}

function timezoneSuggestions(current: string): string[] {
  const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] })
    .supportedValuesOf;
  let names: string[] = [];
  try {
    names = supported ? supported("timeZone") : [];
  } catch {
    names = [];
  }
  return Array.from(new Set([current, ...names]));
}

interface Props {
  initial: BusinessSettings;
  refresh: () => Promise<BusinessSettings | undefined>;
}

export function BusinessProfileForm({ initial, refresh }: Props) {
  const [base, setBase] = useSettingsBase(initial);
  const save = useBusinessSettingsSave(base);
  const [pendingChanges, setPendingChanges] = useState<BusinessSettingsChanges | null>(null);
  const suggestions = useMemo(() => timezoneSuggestions(base.timezone), [base.timezone]);

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

  const commit = (toSave: BusinessSettingsChanges) =>
    save.mutate(toSave, {
      onSuccess: (saved) => {
        setBase(saved);
        reset(toValues(saved));
      },
      onError: (error) => applyServerFieldErrors(error, setError, FIELDS),
    });

  const submit = handleSubmit((values) => {
    const toSave = buildChanges(values, base);
    if (Object.keys(toSave).length === 0) return;
    if (toSave.timezone !== undefined) {
      setPendingChanges(toSave); // confirm first (see dialog below)
      return;
    }
    commit(toSave);
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
    <>
      <form className="flex max-w-lg flex-col gap-4" onSubmit={submit} noValidate>
        <FormField id="name" label="Business name" error={errors.name?.message}>
          <Input id="name" autoComplete="organization" {...register("name")} />
        </FormField>
        <FormField id="business_type" label="Business type" error={errors.business_type?.message}>
          <Input
            id="business_type"
            placeholder="For example, home bakery"
            {...register("business_type")}
          />
        </FormField>
        <FormField id="contact_phone" label="Contact phone" error={errors.contact_phone?.message}>
          <Input id="contact_phone" type="tel" autoComplete="tel" {...register("contact_phone")} />
        </FormField>
        <FormField id="contact_email" label="Contact email" error={errors.contact_email?.message}>
          <Input
            id="contact_email"
            type="email"
            autoComplete="email"
            {...register("contact_email")}
          />
        </FormField>
        <FormField id="address_text" label="Address" error={errors.address_text?.message}>
          <Textarea id="address_text" autoComplete="street-address" {...register("address_text")} />
        </FormField>
        <FormField id="timezone" label="Business timezone" error={errors.timezone?.message}>
          <Input
            id="timezone"
            list="timezone-suggestions"
            autoComplete="off"
            spellCheck={false}
            aria-describedby="timezone-help"
            {...register("timezone")}
          />
          <datalist id="timezone-suggestions">
            {suggestions.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
          <p id="timezone-help" className="text-xs text-muted-foreground">
            An IANA timezone such as America/New_York. Changing it affects how existing dates and
            times are interpreted; it never rewrites saved history.
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

      <AlertDialog
        open={pendingChanges !== null}
        onOpenChange={(open) => {
          if (!open) setPendingChanges(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Change the business timezone?</AlertDialogTitle>
            <AlertDialogDescription>
              This may change how existing fulfillment dates and times are interpreted, what counts
              as "today", Dashboard and production timing, Shopping List business-local dates, and
              how Analytics groups periods. Saved history (completed orders, costs and production
              records) is not rewritten.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep current timezone</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (pendingChanges) commit(pendingChanges);
                setPendingChanges(null);
              }}
            >
              Change timezone
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
