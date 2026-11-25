import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, it, vi, describe } from "vitest";
import * as api from "@/api/creator";
import HomePage from "@/pages/HomePage";
import { installMockFetch } from "@/test/mockFetch";
import userEvent from "@testing-library/user-event";
import InspirationExamples from "../../components/creator/InspirationExamples";
import { useState } from "react";
import VideoTemplatePicker from "../../components/creator/VideoTemplatePicker";

it("keeps the latest sort when an earlier list arrives last", async () => {
  installMockFetch([
    { match: "/models/config", response: { ok: false, status: 503 } },
  ]);
  type List = Awaited<ReturnType<typeof api.listProjects>>;
  const row = (name: string): List["items"][number] => ({
    scenario: "general",
    aspectRatio: "16:9",
    resolution: "1080p",
    projectId: name,
    name,
    description: "",
    createdAt: "2026-11-01T00:00:00Z",
    updatedAt: "2026-11-01T00:00:00Z",
    status: "IDLE",
  });
  const page = (name: string): List => ({
    items: [row(name)],
    limit: 100,
    offset: 0,
  });
  let completeOld!: (value: List) => void;
  const first = new Promise<List>((resolve) => {
    completeOld = resolve;
  });
  const list = vi
    .spyOn(api, "listProjects")
    .mockImplementation((_limit, _offset, sort) =>
      sort === "name" ? Promise.resolve(page("Latest sort")) : first,
    );
  try {
    render(
      <MemoryRouter initialEntries={["/?view=projects"]}>
        <HomePage />
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByRole("combobox"), {
      target: { value: "name" },
    });
    await screen.findByRole("heading", { name: "Latest sort" });
    await act(async () => {
      completeOld(page("Obsolete sort"));
    });
    expect(screen.queryByRole("heading", { name: "Obsolete sort" })).toBeNull();
    expect(
      screen.getByRole("heading", { name: "Latest sort" }),
    ).toBeInTheDocument();
    expect(list.mock.calls.map(([, , sort]) => sort)).toEqual([
      "updated_at",
      "name",
    ]);
  } finally {
    list.mockRestore();
  }
});

it("reloads the list after a project disappears while the same GET is in flight", async () => {
  const items = [
    {
      projectId: "gone",
      name: "Gone",
      createdAt: "2026-11-01",
      updatedAt: "2026-11-01",
    },
  ];
  const { fetchMock } = installMockFetch([
    {
      match: "/projects/gone/copy",
      response: { ok: false, status: 404, json: {} },
    },
    { match: "/projects?", response: { json: { items } } },
    { match: "/models/config", response: { ok: false, status: 503 } },
  ]);
  let finishOld!: (value: Response) => void;
  const old = new Promise<Response>((resolve) => {
    finishOld = resolve;
  });
  let sortedReads = 0;
  vi.stubGlobal("fetch", (input: RequestInfo | URL, options?: RequestInit) => {
    if (!String(input).includes("sort_by=name"))
      return fetchMock(input, options);
    sortedReads++;
    return sortedReads === 1
      ? old
      : Promise.resolve(Response.json({ items: [] }));
  });
  render(
    <MemoryRouter initialEntries={["/?view=projects"]}>
      <HomePage />
    </MemoryRouter>,
  );
  await screen.findByRole("heading", { name: "Gone" });
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "name" } });
  expect(sortedReads).toBe(1);
  fireEvent.click(screen.getByRole("button", { name: /复制.*Gone/ }));
  await vi.waitFor(() => expect(sortedReads).toBe(2));
  await act(async () => {
    finishOld(Response.json({ items }));
  });
  expect(screen.queryByRole("heading", { name: "Gone" })).toBeNull();
});

const EXAMPLE = {
  id: "rainy-day-umbrella",
  title: "短剧制作",
  description: "做一个10秒左右的温馨短剧《雨天送伞》。",
  projectId: "project-af8e135859635dd9ad007dfd88434ba9",
  installed: false,
};

describe("InspirationExamples", () => {
  it("renders the bundled catalogue and opens an example into its project", async () => {
    const { calls } = installMockFetch([
      {
        match: "/examples/rainy-day-umbrella/open",
        method: "POST",
        response: {
          json: { projectId: EXAMPLE.projectId, installed: true },
        },
      },
      { match: "/examples", response: { json: { items: [EXAMPLE] } } },
    ]);
    render(
      <MemoryRouter>
        <Routes>
          <Route path="/" element={<InspirationExamples />} />
          <Route
            path={"/project/" + EXAMPLE.projectId + "/plan"}
            element={<h1>示例已打开</h1>}
          />
        </Routes>
      </MemoryRouter>,
    );

    const card = await screen.findByRole("button", { name: /短剧制作/ });
    await userEvent.click(card);

    await screen.findByRole("heading", { name: "示例已打开" });
    const opens = calls.filter((call) => call.method === "POST");
    expect(opens).toHaveLength(1);
    expect(opens[0].url).toContain("/examples/rainy-day-umbrella/open");
  });

  it("surfaces an error and re-enables the card when opening fails", async () => {
    installMockFetch([
      {
        match: "/examples/rainy-day-umbrella/open",
        method: "POST",
        response: { ok: false, status: 500, json: { message: "boom" } },
      },
      { match: "/examples", response: { json: { items: [EXAMPLE] } } },
    ]);
    render(
      <MemoryRouter>
        <InspirationExamples />
      </MemoryRouter>,
    );

    const card = await screen.findByRole("button", { name: /短剧制作/ });
    await userEvent.click(card);

    await waitFor(() =>
      expect(
        screen.getByText("打开灵感示例失败，请稍后重试"),
      ).toBeInTheDocument(),
    );
    expect(card).toBeEnabled();
  });
});

function template(overrides: Record<string, unknown>) {
  return {
    templateId: "vlog_daily",
    name: "日常Vlog",
    description: "",
    contentType: "travel",
    scenario: "video_edit",
    colorGrade: "vlog_fresh",
    defaultTransitionKind: "dissolve",
    previewDescription: "",
    iconEmoji: "📷",
    captionBlueprints: [],
    energy: "mid",
    density: "mid",
    decoration: "low",
    source: "builtin",
    ...overrides,
  };
}

const TEMPLATES = [
  template({}),
  template({ templateId: "informal_launch", name: "非正式发布会" }),
  template({
    templateId: "custom_1",
    name: "我的风格",
    iconEmoji: "🎨",
    source: "user",
  }),
];

function Harness() {
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <VideoTemplatePicker
      selectedTemplateId={selected}
      onTemplateSelect={setSelected}
    />
  );
}

describe("VideoTemplatePicker", () => {
  it("selects a protected built-in template and returns to the default", async () => {
    installMockFetch([
      { match: "/video-templates", response: { json: { items: TEMPLATES } } },
    ]);
    const { container } = render(<Harness />);

    const pill = container.querySelector("[data-style-entry]")!;
    await userEvent.click(pill);
    await waitFor(() =>
      expect(document.querySelector("[data-style-popover]")).toBeTruthy(),
    );
    const [defaultCard, builtinCard] =
      document.querySelectorAll("[data-style-card]");
    expect(defaultCard).toHaveAttribute("data-style-card", "default");
    expect(defaultCard).toHaveAttribute("data-style-selected");
    expect(builtinCard).toHaveAttribute("data-style-card", "informal_launch");
    expect(builtinCard.querySelector("img")).toBeTruthy();
    expect(builtinCard.querySelector("[data-style-card-delete]")).toBeNull();
    const userCard = document.querySelector('[data-style-card="custom_1"]')!;
    expect(userCard.querySelector("img")).toBeNull();
    expect(userCard).toHaveTextContent("🎨");
    expect(userCard.querySelector("[data-style-card-delete]")).toBeTruthy();

    await userEvent.click(builtinCard);
    await waitFor(() => expect(pill).toHaveTextContent("非正式发布会"));

    await userEvent.click(pill);
    await waitFor(() =>
      expect(
        document.querySelector('[data-style-card="informal_launch"]'),
      ).toHaveAttribute("data-style-selected"),
    );
    await userEvent.click(
      document.querySelector('[data-style-card="default"]')!,
    );
    await waitFor(() => expect(pill).toHaveTextContent("风格: 默认风格"));
  });

  it("deletes a user template after confirm and clears its selection", async () => {
    const { calls } = installMockFetch([
      {
        match: "/video-templates/custom_1",
        method: "DELETE",
        response: { json: { deleted: "custom_1" } },
      },
      { match: "/video-templates", response: { json: { items: TEMPLATES } } },
    ]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { container } = render(<Harness />);

    const pill = container.querySelector("[data-style-entry]")!;
    await userEvent.click(pill);
    await waitFor(() =>
      expect(
        document.querySelector('[data-style-card="custom_1"]'),
      ).toBeTruthy(),
    );
    await userEvent.click(
      document.querySelector('[data-style-card="custom_1"]')!,
    );
    await waitFor(() => expect(pill).toHaveTextContent("风格: 我的风格"));

    await userEvent.click(pill);
    await userEvent.click(document.querySelector("[data-style-card-delete]")!);

    await waitFor(() =>
      expect(
        calls.some(
          (call) =>
            call.method === "DELETE" &&
            call.url.includes("/video-templates/custom_1"),
        ),
      ).toBe(true),
    );
    await waitFor(() => expect(pill).toHaveTextContent("风格: 默认风格"));
  });
});
