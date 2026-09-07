import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { routes } from "../src/app/routes";

function renderAt(initialPath: string) {
  const router = createMemoryRouter(routes, { initialEntries: [initialPath] });
  const queryClient = new QueryClient();
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("app router", () => {
  it("renders the login placeholder at /login", () => {
    renderAt("/login");
    expect(screen.getByText(/login/i)).toBeInTheDocument();
  });

  it("renders the nested dashboard placeholder under the /app layout", () => {
    renderAt("/app/dashboard");
    expect(screen.getByText(/dashboard/i)).toBeInTheDocument();
  });

  it("redirects / to /login", () => {
    renderAt("/");
    expect(screen.getByText(/login/i)).toBeInTheDocument();
  });
});
