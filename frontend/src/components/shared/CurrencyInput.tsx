// New for Phase 6 (Final Plan §I) — a plain text Input with a "$" affix, centralizing
// the money-field pattern that was previously copy-pasted per field. Built fresh, not
// retrofitted onto existing Phase 3-5 forms (Final Pre-Implementation Amendment §20's
// "no opportunistic retrofit" instruction) — those keep their own inline `Input`s.
//
// The value stays a plain string (matching the API's Decimal-as-string wire format,
// same convention as every other money field in the app) — this component only adds
// the visual "$" affix, it does not coerce to a JS number.

import * as React from "react";
import { Input } from "../ui/input";
import { cn } from "cn";

export function CurrencyInput({
  className,
  ...props
}: React.ComponentProps<typeof Input>) {
  return (
    <div className="relative flex items-center">
      <span className="pointer-events-none absolute left-2.5 text-sm text-muted-foreground">
        $
      </span>
      <Input
        type="text"
        inputMode="decimal"
        className={cn("pl-6", className)}
        {...props}
      />
    </div>
  );
}
