import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConfigProvider } from "antd";
import SkillMarketBrowser from "../SkillMarketBrowser";
import type { MarketSkillResult } from "@/api/creator/skills";
import zh from "@/locales/zh.json";

// Controllable market API: paging is decided by the (query, page) pair the
// component sends, so tests read the call log instead of the rendered rows.
const { searchMarketMock, importUrlMock } = vi.hoisted(() => ({
  searchMarketMock: vi.fn(),
  importUrlMock: vi.fn(),
}));

vi.mock("@/api/creator", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/creator")>();
  return {
    ...actual,
    searchMarketSkills: searchMarketMock,
    importSkillFromUrl: importUrlMock,
  };
});

const LANG = "zh";
const PAGE_SIZE = 20;

function marketItem(slug: string): MarketSkillResult {
  return {
    source: "qwenpaw",
    source_label: null,
    slug,
    name: slug,
    description: `${slug} description`,
    source_url: `https://example.test/${slug}`,
    version: null,
    author: null,
    icon_url: null,
    stats: null,
  };
}

function searchPage(items: MarketSkillResult[], hasMore: boolean) {
  return Promise.resolve({
    results: items,
    has_more: hasMore,
    totals: { qwenpaw: items.length },
  });
}

/** motion:false makes the antd Modal mount/unmount synchronously under jsdom. */
function renderBrowser(installedNames: string[] = []) {
  const onInstalled = vi.fn();
  render(
    <ConfigProvider theme={{ token: { motion: false } }}>
      <SkillMarketBrowser
        open
        onClose={vi.fn()}
        onInstalled={onInstalled}
        installedNames={installedNames}
      />
    </ConfigProvider>,
  );
  return { onInstalled };
}

function searchBox(): HTMLElement {
  return screen.getByPlaceholderText(zh.skills.marketSearchPlaceholder);
}

// antd renders a two-CJK-character button label with a space ("搜索" shows as
// "搜 索"), so match the accessible name with optional whitespace per char.
// Anchored, because "安装" is a substring of "已安装".
function dialogButton(label: string): RegExp {
  return new RegExp(`^${label.split("").join("\\s*")}$`);
}

function buttonsByLabel(label: string): HTMLElement[] {
  // Query, not getAll: a test asserts the absence of an install action.
  return within(screen.getByRole("dialog")).queryAllByRole("button", {
    name: dialogButton(label),
  });
}

function buttonByLabel(label: string): HTMLElement {
  return within(screen.getByRole("dialog")).getByRole("button", {
    name: dialogButton(label),
  });
}

/** The n-th row's install action, failing loudly when it is not rendered. */
function installButton(index = 0): HTMLElement {
  const buttons = buttonsByLabel(zh.skills.marketInstall);
  expect(buttons.length).toBeGreaterThan(index);
  return buttons[index];
}

/** Submit the current input draft as a search. */
async function submitSearch(term: string): Promise<void> {
  // The draft must be flushed through a render before the click, otherwise
  // the click would still run against the previous `query` closure.
  await typeDraft(term);
  await act(async () => {
    fireEvent.click(buttonByLabel(zh.common.search));
  });
}

/** Edit the input without submitting it. */
async function typeDraft(term: string): Promise<void> {
  await act(async () => {
    fireEvent.change(searchBox(), { target: { value: term } });
  });
  expect(searchBox()).toHaveValue(term);
}

async function clickLoadMore(): Promise<void> {
  await act(async () => {
    fireEvent.click(buttonByLabel(zh.common.more));
  });
}

function pageRequests(): string[] {
  return searchMarketMock.mock.calls.map(([, page]) => `${String(page)}`);
}

describe("SkillMarketBrowser paging", () => {
  beforeEach(() => {
    searchMarketMock.mockReset();
    importUrlMock.mockReset();
    // Every test starts with the browse request the modal fires on open.
    searchMarketMock.mockImplementation((query: string, page: number) =>
      searchPage([marketItem(`p${page}-${query || "browse"}`)], true),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("requests page 2 for the term whose results are on screen", async () => {
    renderBrowser();
    await submitSearch("alpha");
    expect(searchMarketMock).toHaveBeenLastCalledWith(
      "alpha",
      1,
      PAGE_SIZE,
      LANG,
    );

    // Type a new term but do not submit it, then page. The draft must not
    // leak into the request: page 2 has to belong to "alpha".
    await typeDraft("beta");
    await clickLoadMore();

    expect(searchMarketMock).toHaveBeenLastCalledWith(
      "alpha",
      2,
      PAGE_SIZE,
      LANG,
    );
    // And the rows stay a single-term list: alpha page 1 + alpha page 2.
    expect(screen.getByText("p1-alpha")).toBeVisible();
    expect(screen.getByText("p2-alpha")).toBeVisible();
    expect(screen.queryByText("p1-beta")).toBeNull();
  });

  it("retries the page that failed instead of skipping it", async () => {
    let failPageTwo = true;
    searchMarketMock.mockImplementation((query: string, page: number) => {
      if (page === 1) {
        return searchPage([marketItem(`p1-${query}`)], true);
      }
      if (failPageTwo) {
        return Promise.reject(new Error("market unavailable"));
      }
      return searchPage([marketItem(`p2-${query}`)], false);
    });
    renderBrowser();
    await submitSearch("alpha");

    await clickLoadMore(); // page 2 fails
    expect(screen.queryByText("p2-alpha")).toBeNull();

    failPageTwo = false;
    await clickLoadMore(); // must retry page 2, not jump to page 3

    expect(pageRequests().filter((page) => page === "2")).toEqual(["2", "2"]);
    expect(searchMarketMock).toHaveBeenLastCalledWith(
      "alpha",
      2,
      PAGE_SIZE,
      LANG,
    );
    expect(screen.getByText("p1-alpha")).toBeVisible();
    expect(screen.getByText("p2-alpha")).toBeVisible();
  });

  it("starts a new term over at page 1 and drops the previous results", async () => {
    renderBrowser();
    await submitSearch("alpha");
    await clickLoadMore();
    expect(screen.getByText("p2-alpha")).toBeVisible();

    await submitSearch("beta");

    expect(searchMarketMock).toHaveBeenLastCalledWith(
      "beta",
      1,
      PAGE_SIZE,
      LANG,
    );
    // alpha's two pages are gone, so the next page request is beta page 2.
    expect(screen.queryByText("p1-alpha")).toBeNull();
    expect(screen.queryByText("p2-alpha")).toBeNull();

    await clickLoadMore();
    expect(searchMarketMock).toHaveBeenLastCalledWith(
      "beta",
      2,
      PAGE_SIZE,
      LANG,
    );
    expect(screen.getByText("p1-beta")).toBeVisible();
    expect(screen.getByText("p2-beta")).toBeVisible();
  });

  it("keeps paging from the last good term when a new search fails", async () => {
    let rejectAlpha = false;
    searchMarketMock.mockImplementation((query: string, page: number) => {
      if (rejectAlpha && query === "alpha") {
        return Promise.reject(new Error("market unavailable"));
      }
      return searchPage([marketItem(`p${page}-${query || "browse"}`)], true);
    });
    renderBrowser();
    await submitSearch("beta");

    // A failed search must not re-commit the term or reset the page: what is
    // on screen is still beta, so "load more" continues beta.
    rejectAlpha = true;
    await submitSearch("alpha");
    expect(screen.queryByText("p1-alpha")).toBeNull();

    rejectAlpha = false;
    await clickLoadMore();
    expect(searchMarketMock).toHaveBeenLastCalledWith(
      "beta",
      2,
      PAGE_SIZE,
      LANG,
    );
  });

  it("installs through the item source url and marks it installed", async () => {
    importUrlMock.mockResolvedValue({ ok: true, name: "p1-alpha" });
    const { onInstalled } = renderBrowser();
    await submitSearch("alpha");

    await act(async () => {
      fireEvent.click(installButton());
    });

    expect(importUrlMock).toHaveBeenCalledWith("https://example.test/p1-alpha");
    expect(onInstalled).toHaveBeenCalled();
    expect(
      within(screen.getByRole("dialog")).queryByText(zh.skills.marketInstalled),
    ).not.toBeNull();
    expect(buttonByLabel(zh.skills.marketInstalled)).toBeDisabled();
  });

  it("does not offer an install for a skill already in the library", async () => {
    renderBrowser(["p1-alpha"]);
    await submitSearch("alpha");

    expect(buttonsByLabel(zh.skills.marketInstall)).toHaveLength(0);
    expect(buttonByLabel(zh.skills.marketInstalled)).toBeDisabled();
    expect(importUrlMock).not.toHaveBeenCalled();
  });
});
