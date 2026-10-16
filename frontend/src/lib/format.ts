// Display-only formatting helpers shared by the Phase 9 Dashboard and Shopping List
// (Spec §12.19/§12.20). Nothing here computes a business value — the backend is
// authoritative (Spec §12.5); these only turn server-provided values into readable text.
//
// Dates (`YYYY-MM-DD`) and wall-clock times (`HH:MM[:SS]`) from the API are already
// Business-local, so they are formatted by string/UTC arithmetic and are never routed
// through the browser's local timezone. Instants (ISO timestamps) are rendered in the
// Business timezone explicitly (§12.20), never the viewer's.

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** `2026-06-10` -> `Wed, Jun 10`. */
export function formatDate(isoDate: string): string {
  const [year, month, day] = isoDate.split("-").map(Number);
  const weekday = WEEKDAYS[new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
  return `${weekday}, ${MONTHS[month - 1]} ${day}`;
}

/** `09:30:00` -> `9:30 AM`. A null time is never invented — callers decide the wording. */
export function formatTime(time: string): string {
  const [hourText, minuteText] = time.split(":");
  const hour = Number(hourText);
  const suffix = hour >= 12 ? "PM" : "AM";
  const twelve = hour % 12 === 0 ? 12 : hour % 12;
  return `${twelve}:${minuteText} ${suffix}`;
}

/** An ISO instant rendered as a short date/time in the given IANA timezone. */
export function formatInstantInTimezone(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone,
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(iso));
}

/** `85.5` / `85.50` -> `$85.50` (the API already supplies two-decimal money strings). */
export function formatMoney(value: string): string {
  const negative = value.startsWith("-");
  const unsigned = negative ? value.slice(1) : value;
  const [integerPart, fractionalPart = "00"] = unsigned.split(".");
  const grouped = integerPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}$${grouped}.${fractionalPart.padEnd(2, "0").slice(0, 2)}`;
}

/** `135` -> `2 h 15 min`; `45` -> `45 min`. */
export function formatMinutes(total: number): string {
  if (total < 60) {
    return `${total} min`;
  }
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  return minutes === 0 ? `${hours} h` : `${hours} h ${minutes} min`;
}
