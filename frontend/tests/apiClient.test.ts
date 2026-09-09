// Checkpoint 2 review, issue 4 (+ hardening pass): `apiFetch` must merge the caller's
// RequestInit onto its own required behavior, not the other way around — a caller-supplied
// `headers` (in any valid HeadersInit shape: plain object, Headers instance, or tuple
// array) must never be able to override the session-bound CSRF header, and a
// caller-supplied `credentials` must never override "include". Headers are built via the
// `Headers` API specifically so all three HeadersInit shapes are handled uniformly.

import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "../src/api/client";

function setCsrfCookie(value: string) {
  document.cookie = `fo_csrf=${value}; path=/`;
}

function clearCsrfCookie() {
  document.cookie = "fo_csrf=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/";
}

function stubFetchCapturingInit(): { getInit: () => RequestInit | undefined } {
  let capturedInit: RequestInit | undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((_url: string, init: RequestInit) => {
      capturedInit = init;
      return Promise.resolve(
        new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }),
  );
  return { getInit: () => capturedInit };
}

describe("apiFetch option merge", () => {
  afterEach(() => {
    clearCsrfCookie();
    vi.unstubAllGlobals();
  });

  it("preserves the automatic CSRF header alongside caller-supplied plain-object headers", async () => {
    setCsrfCookie("test-csrf-token");
    const { getInit } = stubFetchCapturingInit();

    await apiFetch("/api/v1/auth/logout", {
      method: "POST",
      headers: { "X-Custom-Header": "custom-value" },
    });

    const headers = getInit()!.headers as Headers;
    expect(headers.get("X-CSRF-Token")).toBe("test-csrf-token");
    expect(headers.get("X-Custom-Header")).toBe("custom-value");
    expect(headers.get("Content-Type")).toBe("application/json");
  });

  it("preserves custom headers supplied as a Headers instance", async () => {
    setCsrfCookie("headers-instance-token");
    const { getInit } = stubFetchCapturingInit();

    await apiFetch("/api/v1/auth/logout", {
      method: "POST",
      headers: new Headers({ "X-Custom-Header": "from-headers-instance" }),
    });

    const headers = getInit()!.headers as Headers;
    expect(headers.get("X-CSRF-Token")).toBe("headers-instance-token");
    expect(headers.get("X-Custom-Header")).toBe("from-headers-instance");
  });

  it("preserves custom headers supplied as a tuple array", async () => {
    setCsrfCookie("tuple-array-token");
    const { getInit } = stubFetchCapturingInit();

    await apiFetch("/api/v1/auth/logout", {
      method: "POST",
      headers: [["X-Custom-Header", "from-tuple-array"]],
    });

    const headers = getInit()!.headers as Headers;
    expect(headers.get("X-CSRF-Token")).toBe("tuple-array-token");
    expect(headers.get("X-Custom-Header")).toBe("from-tuple-array");
  });

  it("replaces a caller-supplied fake X-CSRF-Token with the real session-bound token", async () => {
    setCsrfCookie("the-real-csrf-token");
    const { getInit } = stubFetchCapturingInit();

    await apiFetch("/api/v1/auth/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": "a-forged-value-from-the-caller" },
    });

    const headers = getInit()!.headers as Headers;
    expect(headers.get("X-CSRF-Token")).toBe("the-real-csrf-token");
  });

  it("does not let a caller override credentials away from 'include'", async () => {
    const { getInit } = stubFetchCapturingInit();

    // "omit" is a perfectly valid RequestCredentials value — no @ts-expect-error needed;
    // the point being tested is purely runtime behavior (apiFetch always forces "include"),
    // not a type-level conflict.
    await apiFetch("/health", { credentials: "omit" });

    expect(getInit()!.credentials).toBe("include");
  });

  it("preserves an explicitly supplied Content-Type instead of overwriting it", async () => {
    const { getInit } = stubFetchCapturingInit();

    await apiFetch("/api/v1/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
    });

    const headers = getInit()!.headers as Headers;
    expect(headers.get("Content-Type")).toBe("application/x-www-form-urlencoded");
  });
});
