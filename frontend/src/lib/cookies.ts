// Minimal client-side cookie reader. Only used for the non-HttpOnly `fo_csrf` cookie
// (Phase 2 plan §3) — the session cookie itself is HttpOnly and never readable from JS.
export function getCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}
