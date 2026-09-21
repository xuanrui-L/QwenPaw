import { afterEach, describe, expect, it, vi } from "vitest";
import { installMockFetch } from "@/test/mockFetch";
import {
  applyPlatformCredentials,
  fetchPlatformCredentials,
  PLATFORM_CREDENTIALS_PATH,
} from "@/api/creator/platform";

const CREDENTIALS_ROUTE = "creator-credentials";

function credentialPayload(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      user_id: "u-1",
      deployment_id: "d-1",
      api_key: "sk-as-issued",
      api_key_created: true,
      display_available_credits: 120,
      chat_completions_url:
        "https://platform-pre.agentscope.io/v1/chat/completions",
      auth_header: "Authorization: Bearer sk-as-issued",
      ...overrides,
    },
  };
}

describe("platform credential fetch", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("asks the subdomain gateway with the cookie and no bearer", async () => {
    const { calls, fetchMock } = installMockFetch([
      { match: CREDENTIALS_ROUTE, response: { json: credentialPayload() } },
    ]);

    const credentials = await fetchPlatformCredentials();

    expect(credentials.api_key).toBe("sk-as-issued");
    // Relative on purpose: the endpoint only exists behind this deployment's
    // subdomain, and an absolute platform URL would carry no console_token.
    expect(calls[0]).toMatchObject({
      url: PLATFORM_CREDENTIALS_PATH,
      method: "GET",
    });
    expect(PLATFORM_CREDENTIALS_PATH).toBe(
      "/api/v1/qwenpaw/creator-credentials",
    );
    // The platform authenticates on console_token alone: its own JWT is
    // refused and Creator's bearer is ignored, so sending either would only
    // make a failed lookup look like an authenticated one.
    expect(calls[0].headers.Authorization).toBeUndefined();
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      credentials: "include",
    });
  });

  it("refuses a response that carries no key instead of storing an empty one", async () => {
    installMockFetch([
      {
        match: CREDENTIALS_ROUTE,
        response: { json: credentialPayload({ api_key: "  " }) },
      },
    ]);

    await expect(fetchPlatformCredentials()).rejects.toThrow(/api_key/);
  });

  it("names the missing session on 401 rather than a generic failure", async () => {
    installMockFetch([
      {
        match: CREDENTIALS_ROUTE,
        response: { ok: false, status: 401, json: {} },
      },
    ]);

    await expect(fetchPlatformCredentials()).rejects.toThrow(/console_token/);
  });

  it("hands the key and endpoint to the Creator backend to persist", async () => {
    const { calls } = installMockFetch([
      {
        match: "platform-autoconfigure",
        method: "POST",
        response: {
          json: {
            ok: true,
            base_url: "https://platform-pre.agentscope.io/v1",
            sections: [
              { section: "llm", model_name: "qwen3.8-flash", ready: true },
            ],
          },
        },
      },
    ]);

    const result = await applyPlatformCredentials({
      api_key: "sk-as-issued",
      chat_completions_url:
        "https://platform-pre.agentscope.io/v1/chat/completions",
    });

    expect(result.sections[0]).toMatchObject({ ready: true });
    expect(calls[0].body).toEqual({
      api_key: "sk-as-issued",
      chat_completions_url:
        "https://platform-pre.agentscope.io/v1/chat/completions",
    });
  });
});
