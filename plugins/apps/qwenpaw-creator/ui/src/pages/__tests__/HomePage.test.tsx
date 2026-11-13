import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import HomePage from "@/pages/HomePage";
import { CreatorHttpError } from "@/api/creator";
import { NavigationRuntime } from "@/routing/navigation";
import i18n from "@/i18n";

const api = vi.hoisted(() => ({
  listProjects: vi.fn(),
  listTasks: vi.fn(),
  getStorageProfile: vi.fn(),
}));
vi.mock("@/api/creator", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  ...api,
}));
vi.mock("@/app/theme", () => ({
  useTheme: () => ({ resolvedTheme: "light" }),
}));
vi.mock("@/store/modelConfigStore", () => {
  const state = { config: null, refresh: vi.fn() };
  return {
    useModelConfigStore: (selector: (value: typeof state) => unknown) =>
      selector(state),
  };
});
vi.mock("@/components/creator/ModelBadges", () => ({ default: () => null }));
vi.mock("@/components/creator/ModelConfigModal", () => ({
  default: () => null,
}));
vi.mock("@/components/creator/HeroComposerCard", () => ({
  default: () => null,
}));
vi.mock("@/components/creator/InspirationExamples", () => ({
  default: () => null,
}));
const completion = vi.hoisted(() => ({
  imported: undefined as (() => void) | undefined,
}));
vi.mock("@/components/creator/ProjectImportExport", () => ({
  ProjectImporter: ({ onImported }: { onImported: () => void }) => {
    completion.imported = onImported;
    return null;
  },
}));
vi.mock("@/components/onboarding", () => ({ HomeTour: () => null }));

const project = {
  projectId: "project-existing",
  name: "Existing project",
  description: "",
  scenario: "general",
  aspectRatio: "16:9",
  resolution: "720P",
  contentType: null,
  createdAt: "2026-01-01T00:00:00Z",
  updatedAt: "2026-01-01T00:00:00Z",
  status: "IDLE",
};

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/?view=projects"]}>
      <NavigationRuntime />
      <HomePage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  api.listProjects.mockResolvedValue({ items: [] });
  api.listTasks.mockResolvedValue({ items: [] });
  api.getStorageProfile.mockResolvedValue({
    projects: [],
    autoReclaimMaxBytes: 0,
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("project list recovery", () => {
  it("shows the actual error, retries and keeps existing cards after a later failure", async () => {
    api.listProjects.mockRejectedValueOnce(
      new CreatorHttpError(500, {
        message: "Storage unavailable",
        errorId: "error-test",
      }),
    );
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent("error-test");
    expect(
      screen.queryByText(i18n.t("home.noProjects")),
    ).not.toBeInTheDocument();
    api.listProjects.mockResolvedValueOnce({ items: [project] });
    fireEvent.click(
      screen.getByRole("button", { name: i18n.t("common.retry") }),
    );
    expect(await screen.findByText("Existing project")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    api.listProjects.mockRejectedValueOnce(new Error("Connection lost"));
    fireEvent.change(
      screen.getByRole("combobox", { name: i18n.t("home.sortBy") }),
      { target: { value: "name" } },
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Connection lost",
    );
    expect(screen.getByText("Existing project")).toBeInTheDocument();
    expect(api.listProjects).toHaveBeenCalledTimes(3);
  });

  it("recovers automatically only while the runtime is preparing", async () => {
    vi.useFakeTimers();
    api.listProjects
      .mockRejectedValueOnce(
        new CreatorHttpError(503, { code: "RUNTIME_PREPARING" }),
      )
      .mockResolvedValue({ items: [project] });
    await act(async () => {
      renderPage();
    });
    expect(screen.getByRole("alert")).toHaveTextContent(
      i18n.t("home.preparingProjects"),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(screen.getByText("Existing project")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(api.listProjects).toHaveBeenCalledTimes(2);
  });

  it("bounds recovery retries and cancels them on unmount", async () => {
    vi.useFakeTimers();
    api.listProjects.mockRejectedValue(
      new CreatorHttpError(503, { code: "RUNTIME_PREPARING" }),
    );
    let mounted: ReturnType<typeof renderPage>;
    await act(async () => {
      mounted = renderPage();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(25000);
    });
    expect(api.listProjects).toHaveBeenCalledTimes(11);
    expect(screen.getByRole("alert")).toHaveTextContent(
      i18n.t("home.preparingProjectsTimeout"),
    );
    await act(async () => {
      fireEvent.click(
        screen.getByRole("button", { name: i18n.t("common.retry") }),
      );
    });
    expect(api.listProjects).toHaveBeenCalledTimes(12);
    mounted!.unmount();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20000);
    });
    expect(api.listProjects).toHaveBeenCalledTimes(12);
  });

  it("does not restart requests from an import completion after unmount", async () => {
    vi.useFakeTimers();
    let mounted: ReturnType<typeof renderPage>;
    await act(async () => {
      mounted = renderPage();
    });
    const onImported = completion.imported!;
    mounted!.unmount();
    api.listProjects.mockRejectedValue(
      new CreatorHttpError(503, { code: "RUNTIME_PREPARING" }),
    );
    await act(async () => {
      onImported();
      await vi.advanceTimersByTimeAsync(25000);
    });
    expect(api.listProjects).toHaveBeenCalledTimes(1);
  });

  it("ignores an older response after a new sort has loaded", async () => {
    let finishOld!: (value: { items: (typeof project)[] }) => void;
    api.listProjects
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finishOld = resolve;
          }),
      )
      .mockResolvedValue({ items: [project] });
    renderPage();
    fireEvent.change(
      await screen.findByRole("combobox", { name: i18n.t("home.sortBy") }),
      { target: { value: "name" } },
    );
    await screen.findByText("Existing project");
    await act(async () => {
      finishOld({ items: [{ ...project, name: "Stale project" }] });
    });
    await waitFor(() =>
      expect(screen.queryByText("Stale project")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Existing project")).toBeInTheDocument();
  });
});
