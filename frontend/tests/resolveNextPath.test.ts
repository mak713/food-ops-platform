// Checkpoint 2 review, login return-path bug: resolveNextPath must accept only a safe
// internal /app/* destination and never blindly follow an external/unsafe `next` value
// (open-redirect requirement).

import { describe, expect, it } from "vitest";
import { resolveNextPath } from "../src/features/auth/LoginPage";

describe("resolveNextPath", () => {
  it("returns the dashboard when next is missing", () => {
    expect(resolveNextPath(null)).toBe("/app/dashboard");
  });

  it("returns the dashboard when next is an empty string", () => {
    expect(resolveNextPath("")).toBe("/app/dashboard");
  });

  it("accepts a valid internal /app/* path", () => {
    expect(resolveNextPath("/app/account")).toBe("/app/account");
  });

  it("accepts a valid internal /app/* path with a query string", () => {
    expect(resolveNextPath("/app/account?tab=danger")).toBe("/app/account?tab=danger");
  });

  it("accepts the bare /app path", () => {
    expect(resolveNextPath("/app")).toBe("/app");
  });

  it("falls back to the dashboard for an external absolute URL", () => {
    expect(resolveNextPath("https://evil.example.com")).toBe("/app/dashboard");
  });

  it("falls back to the dashboard for a protocol-relative URL", () => {
    expect(resolveNextPath("//evil.example.com")).toBe("/app/dashboard");
  });

  it("falls back to the dashboard for a javascript: URL", () => {
    expect(resolveNextPath("javascript:alert(1)")).toBe("/app/dashboard");
  });

  it("falls back to the dashboard for a path outside /app", () => {
    expect(resolveNextPath("/login")).toBe("/app/dashboard");
  });

  it("falls back to the dashboard for a path that only shares the /app prefix as a substring", () => {
    expect(resolveNextPath("/appearance")).toBe("/app/dashboard");
  });

  it("falls back to the dashboard for a bare relative path with no leading slash", () => {
    expect(resolveNextPath("app/account")).toBe("/app/dashboard");
  });
});
