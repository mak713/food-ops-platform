// The one global period control (Spec §12.16: "one global date filter"). Presets plus a
// Custom Range whose two dates are both inclusive. The browser only does light UX validation
// (start <= end); the server is authoritative and its structured 422 is shown by the page.

import { useState } from "react";
import { Button } from "../../components/ui/button";
import { Input } from "../../components/ui/input";
import { PRESET_RANGES, RANGE_LABELS, type PeriodSelection } from "./api";

interface Props {
  selection: PeriodSelection;
  onChange: (selection: PeriodSelection) => void;
}

export function DateRangeControl({ selection, onChange }: Props) {
  const [startDate, setStartDate] = useState(selection.startDate ?? "");
  const [endDate, setEndDate] = useState(selection.endDate ?? "");
  const [error, setError] = useState<string | null>(null);

  const applyCustom = () => {
    if (!startDate || !endDate) {
      setError("Choose both a start date and an end date.");
      return;
    }
    if (startDate > endDate) {
      setError("The start date cannot be after the end date.");
      return;
    }
    setError(null);
    onChange({ range: "CUSTOM", startDate, endDate });
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-end gap-2" role="group" aria-label="Reporting period">
        {PRESET_RANGES.map((range) => (
          <Button
            key={range}
            variant={selection.range === range ? "default" : "outline"}
            size="sm"
            aria-pressed={selection.range === range}
            onClick={() => {
              setError(null);
              onChange({ range });
            }}
          >
            {RANGE_LABELS[range]}
          </Button>
        ))}
        <div className="flex flex-col gap-1">
          <label htmlFor="analytics-start" className="text-xs font-medium">
            Start date
          </label>
          <Input
            id="analytics-start"
            type="date"
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
            className="w-40"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="analytics-end" className="text-xs font-medium">
            End date
          </label>
          <Input
            id="analytics-end"
            type="date"
            value={endDate}
            min={startDate || undefined}
            onChange={(e) => setEndDate(e.target.value)}
            className="w-40"
          />
        </div>
        <Button
          variant={selection.range === "CUSTOM" ? "default" : "outline"}
          size="sm"
          aria-pressed={selection.range === "CUSTOM"}
          onClick={applyCustom}
        >
          Apply custom range
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
