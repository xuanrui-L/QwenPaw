import { describe, expect, it, afterEach, beforeEach, vi } from "vitest";
import { matchRoutes } from "react-router-dom";
import { CREATOR_ROUTE_OBJECTS } from "@/app/router";
import { normalizeCreatorRoute } from "@/routing/navigation";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider, useTheme } from "@/app/theme";

function terminalRouteId(path: string): string | undefined {
  const matches = matchRoutes(CREATOR_ROUTE_OBJECTS, path);
  return matches?.at(-1)?.route.id;
}

describe("Creator hash router", () => {
  it("registers the Blueprint, parameterized Plan/Workbench, legacy and Assets page paths", () => {
    expect(terminalRouteId("/")).toBe("home");
    expect(terminalRouteId("/project/p1/t/timeline%3Amain/plan")).toBe(
      "project-timeline-plan",
    );
    expect(
      terminalRouteId("/project/p1/t/timeline%3Amain/plan/element/r2v-window"),
    ).toBe("project-timeline-element-workbench");
    // Legacy paths stay routable: /plan redirects to the primary timeline's
    // parameterized route, the element workbench renders directly.
    expect(terminalRouteId("/project/p1/plan")).toBe("project-plan");
    expect(terminalRouteId("/project/p1/plan/element/r2v-window")).toBe(
      "project-element-workbench",
    );
    expect(terminalRouteId("/project/p1/assets")).toBe("project-assets");
    expect(terminalRouteId("/project/p1/unknown")).toBe("project-not-found");
  });

  it("keeps /project/:id as the blueprint default entry", () => {
    expect(terminalRouteId("/project/p1")).toBe("project-blueprint");
  });

  it("normalizes only safe same-app routes for host URL synchronization", () => {
    expect(normalizeCreatorRoute("/project/p1/plan?reviewOp=op-1")).toBe(
      "/project/p1/plan?reviewOp=op-1",
    );
    expect(normalizeCreatorRoute("/")).toBe("/");
    expect(normalizeCreatorRoute("project/p1/plan")).toBeNull();
    expect(normalizeCreatorRoute("//example.com/project/p1/plan")).toBeNull();
    expect(normalizeCreatorRoute("/project/p1/plan#other")).toBeNull();
  });
});

function ThemeProbe() {
  const { theme, resolvedTheme, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme">{theme}</span>
      <span data-testid="resolved-theme">{resolvedTheme}</span>
      <button type="button" onClick={() => setTheme("dark")}>
        dark
      </button>
    </div>
  );
}

const getItem = vi.fn();
const setItem = vi.fn();

describe("local ThemeProvider", () => {
  beforeEach(() => {
    getItem.mockReset().mockReturnValue(null);
    setItem.mockReset();
    vi.stubGlobal("localStorage", { getItem, setItem });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    document.documentElement.classList.remove("dark", "dark-mode");
  });

  it("persists and applies the selected theme in the Vite app", async () => {
    render(
      <ThemeProvider defaultTheme="light">
        <ThemeProbe />
      </ThemeProvider>,
    );

    expect(screen.getByTestId("theme")).toHaveTextContent("light");
    fireEvent.click(screen.getByRole("button", { name: "dark" }));

    await waitFor(() => {
      expect(screen.getByTestId("resolved-theme")).toHaveTextContent("dark");
      expect(document.documentElement).toHaveClass("dark", "dark-mode");
      expect(setItem).toHaveBeenCalledWith("qwenpaw-theme", "dark");
    });
  });
});
