// Factors out the label+input+error markup every Phase 2 form hand-rolled identically
// (LoginPage, SignupPage, AccountSettingsPage, ...). Phase 3 introduces this as a shared
// component for its own new forms — existing Phase 2 forms are left untouched (Phase 3
// plan v3 §15/§16: scope discipline, not a retrofit).

import type { ReactNode } from "react";

interface FormFieldProps {
  id: string;
  label: string;
  error?: string;
  children: ReactNode;
}

export function FormField({ id, label, error, children }: FormFieldProps) {
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {children}
      {error && (
        <p className="text-xs text-destructive" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
