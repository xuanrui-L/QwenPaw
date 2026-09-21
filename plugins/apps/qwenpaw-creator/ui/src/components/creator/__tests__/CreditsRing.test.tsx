import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import CreditsRing from "../CreditsRing";
import { ringFraction, useCreditsStore } from "@/store/creditsStore";
import { fetchCreditsUsage } from "@/api/creator/platform";
import { installMockFetch } from "@/test/mockFetch";

const CREDENTIALS = "/api/v1/qwenpaw/creator-credentials";
const USAGE = "/api/v1/qwenpaw/creator/credits-usage";

function resetStore() {
  useCreditsStore.setState({
    status: "idle",
    available: null,
    consumed: null,
    byModel: [],
    totalCalls: undefined,
    balanceKnown: false,
  });
}

describe("ringFraction", () => {
  it("is null when the balance is unknown, so no fake ring is drawn", () => {
    expect(ringFraction(null, 50)).toBeNull();
    expect(ringFraction(null, null)).toBeNull();
  });

  it("reads remaining over consumed-plus-remaining", () => {
    expect(ringFraction(75, 25)).toBeCloseTo(0.75);
    expect(ringFraction(0, 100)).toBe(0);
  });

  it("treats a missing consumption as zero", () => {
    expect(ringFraction(40, null)).toBe(1);
  });
});

describe("fetchCreditsUsage", () => {
  it("unwraps the data envelope and coerces numeric fields", async () => {
    installMockFetch([
      {
        match: USAGE,
        response: {
          ok: true,
          status: 200,
          json: {
            request_id: "r1",
            data: {
              total_settled_credits: 128.5,
              total_calls: 42,
              by_model: [
                {
                  model_id: "qwen-image-3.0",
                  model_type: "image",
                  settled_credits: "90.0",
                  call_count: 5,
                },
              ],
            },
          },
        },
      },
    ]);
    const usage = await fetchCreditsUsage();
    expect(usage.total_settled_credits).toBe(128.5);
    expect(usage.by_model[0].settled_credits).toBe(90);
    expect(usage.by_model[0].model_id).toBe("qwen-image-3.0");
  });

  it("throws on a non-2xx rather than returning empty", async () => {
    installMockFetch([
      { match: USAGE, response: { ok: false, status: 502, json: {} } },
    ]);
    await expect(fetchCreditsUsage()).rejects.toThrow(/HTTP 502/);
  });
});

describe("creditsStore.load", () => {
  beforeEach(() => {
    resetStore();
    vi.unstubAllGlobals();
  });

  it("marks error and keeps every other surface alive when both endpoints are unreachable", async () => {
    // The local-test reality: the subdomain platform routes do not resolve.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    await useCreditsStore.getState().load();
    const state = useCreditsStore.getState();
    expect(state.status).toBe("error");
    expect(state.balanceKnown).toBe(false);
    expect(state.available).toBeNull();
  });

  it("still shows the balance when only the usage endpoint fails", async () => {
    installMockFetch([
      {
        match: CREDENTIALS,
        response: {
          ok: true,
          status: 200,
          json: {
            data: {
              api_key: "k",
              chat_completions_url: "u",
              display_available_credits: 60,
            },
          },
        },
      },
      { match: USAGE, response: { ok: false, status: 500, json: {} } },
    ]);
    await useCreditsStore.getState().load();
    const state = useCreditsStore.getState();
    expect(state.status).toBe("ready");
    expect(state.available).toBe(60);
    expect(state.consumed).toBeNull();
    expect(state.byModel).toEqual([]);
  });

  it("populates the per-model detail when both succeed", async () => {
    installMockFetch([
      {
        match: CREDENTIALS,
        response: {
          ok: true,
          status: 200,
          json: {
            data: {
              api_key: "k",
              chat_completions_url: "u",
              display_available_credits: 150,
            },
          },
        },
      },
      {
        match: USAGE,
        response: {
          ok: true,
          status: 200,
          json: {
            data: {
              total_settled_credits: 50,
              by_model: [{ model_id: "wan3.0-video", settled_credits: 50 }],
            },
          },
        },
      },
    ]);
    await useCreditsStore.getState().load();
    const state = useCreditsStore.getState();
    expect(state.status).toBe("ready");
    expect(state.available).toBe(150);
    expect(state.consumed).toBe(50);
    expect(state.byModel).toHaveLength(1);
  });

  it("gates a repeat load() once ready but reload() re-reads the balance", async () => {
    // reload() is the shared primitive behind tab-focus and node-completion
    // refreshes; it must bypass the mount gate that load() enforces.
    const { calls } = installMockFetch([
      {
        match: CREDENTIALS,
        response: {
          ok: true,
          status: 200,
          json: {
            data: {
              api_key: "k",
              chat_completions_url: "u",
              display_available_credits: 100,
            },
          },
        },
      },
    ]);
    await useCreditsStore.getState().load();
    expect(useCreditsStore.getState().available).toBe(100);
    const afterFirst = calls.length;
    await useCreditsStore.getState().load();
    expect(calls.length).toBe(afterFirst);
    await useCreditsStore.getState().reload();
    expect(calls.length).toBeGreaterThan(afterFirst);
  });
});

describe("CreditsRing rendering", () => {
  beforeEach(() => {
    resetStore();
    vi.unstubAllGlobals();
  });

  it("draws a neutral placeholder ring when the endpoint is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    render(<CreditsRing />);
    // The ring element still exists (no crash), just without a value arc.
    expect(await screen.findByRole("img")).toBeInTheDocument();
    expect(document.querySelector("[data-credits-ring]")).not.toBeNull();
  });

  it("does not take its siblings down when the platform route 404s", async () => {
    installMockFetch([
      { match: CREDENTIALS, response: { ok: false, status: 404, json: {} } },
      { match: USAGE, response: { ok: false, status: 404, json: {} } },
    ]);
    render(
      <div>
        <button type="button">other-control</button>
        <CreditsRing />
      </div>,
    );
    await act(async () => {
      await useCreditsStore.getState().load();
    });
    expect(screen.getByText("other-control")).toBeInTheDocument();
    expect(useCreditsStore.getState().status).toBe("error");
  });
});
