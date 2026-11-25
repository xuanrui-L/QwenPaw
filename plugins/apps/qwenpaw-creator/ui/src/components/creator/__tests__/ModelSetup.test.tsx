import * as platformApi from "@/api/creator/platform";
import { afterEach } from "vitest";
import { PLATFORM_CREDENTIALS_PATH } from "@/api/creator/platform";
import {
  IVB_UPLOAD_PATH,
  newUploadIdempotencyKey,
  uploadInteractiveBundle,
} from "@/api/creator/ivbUpload";
import type { HostProviderInfo } from "@/api/creator";
import type { TtsModelCapability } from "@/api/creator";
import {
  PRESETS_BY_TYPE,
  llmPresetFor,
  ttsModelChoices,
} from "../ModelConfigModal";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, beforeEach, vi } from "vitest";
import ModelBadges from "../ModelBadges";
import type { ModelConfigData } from "@/contracts/creator";
import { configuredModelConfig } from "@/test/agentFixtures";
import { installMockFetch } from "@/test/mockFetch";
import PlatformAutoConfigPane from "../PlatformAutoConfigPane";
import {
  applyPlatformCredentials,
  fetchPlatformCredentials,
} from "@/api/creator/platform";

/** Serves the shared configured fixture with optional tts overrides. */
function renderBadges(
  tts: Partial<ModelConfigData["tts"]> = {},
  overrides: Partial<ModelConfigData> = {},
) {
  installMockFetch([
    {
      match: "/models/config",
      method: "GET",
      response: {
        json: {
          ...configuredModelConfig,
          llm: {
            ...configuredModelConfig.llm,
            protocol: "DashScope（百炼）",
            base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
          },
          tts: { ...configuredModelConfig.tts, ...tts },
          ...overrides,
        },
      },
    },
  ]);
  render(<ModelBadges />);
}

describe("platform quick config entry", () => {
  it("opens one-click setup without going through the model dialog", async () => {
    // Wiring guard: the entry lives beside the status badges and is
    // unconditional - a deployment that cannot reach the platform should show
    // a failing button, not a missing one.
    renderBadges();
    fireEvent.click(await screen.findByRole("button", { name: "一键配置" }));

    expect(
      await screen.findByRole("button", { name: "拉取并配置" }),
    ).toBeInTheDocument();
  });
});

describe("PlatformAutoConfigPane", () => {
  const fetchMock = () => vi.mocked(platformApi.fetchPlatformCredentials);
  const applyMock = () => vi.mocked(platformApi.applyPlatformCredentials);

  afterEach(() => vi.restoreAllMocks());

  beforeEach(() => {
    vi.spyOn(platformApi, "fetchPlatformCredentials");
    vi.spyOn(platformApi, "applyPlatformCredentials");
    fetchMock().mockResolvedValue({
      api_key: "sk-as-issued",
      chat_completions_url:
        "https://platform-pre.agentscope.io/v1/chat/completions",
      display_available_credits: 120,
    });
    applyMock().mockResolvedValue({
      ok: true,
      base_url: "https://platform-pre.agentscope.io/v1",
      sections: [
        { section: "llm", model_name: "qwen3.8-flash", ready: true },
        { section: "tts", model_name: "", ready: false },
      ],
    });
  });

  it("carries the issued key straight through to the backend", async () => {
    render(<PlatformAutoConfigPane onJumpToModel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "拉取并配置" }));

    await waitFor(() => expect(applyMock()).toHaveBeenCalledTimes(1));
    // The pane must not re-shape the credential on the way: an edited URL is
    // what turns a working key into an endpoint the proxy does not serve.
    expect(applyMock().mock.calls[0][0]).toMatchObject({
      api_key: "sk-as-issued",
      chat_completions_url:
        "https://platform-pre.agentscope.io/v1/chat/completions",
    });
  });

  it("lists what is configured and what still needs a model", async () => {
    const onJumpToModel = vi.fn();
    render(<PlatformAutoConfigPane onJumpToModel={onJumpToModel} />);
    fireEvent.click(screen.getByRole("button", { name: "拉取并配置" }));

    expect(await screen.findByText("qwen3.8-flash")).toBeInTheDocument();
    const needsModel = screen.getByRole("button", { name: "未选择模型" });
    fireEvent.click(needsModel);
    // A missing model is not this pane's decision to make; it sends the
    // operator to the card where the choice is validated.
    expect(onJumpToModel).toHaveBeenCalledWith("tts");
  });

  it("keeps the failure visible instead of looking like a silent success", async () => {
    fetchMock().mockRejectedValue(new Error("平台未识别本子域的登录会话"));
    render(<PlatformAutoConfigPane onJumpToModel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "拉取并配置" }));

    expect(
      await screen.findByText("平台未识别本子域的登录会话"),
    ).toBeInTheDocument();
    expect(applyMock()).not.toHaveBeenCalled();
    expect(screen.queryByText("qwen3.8-flash")).toBeNull();
  });
});

const platformSpeechModels = [
  {
    model: "qwen3-tts-flash",
    label: "Qwen3 TTS Flash（系统音色，快速）",
    family: "qwen-tts",
    transport: "http",
    systemVoices: ["Cherry", "Ethan"],
    supportsDesign: true,
    providers: ["bailian"],
  },
  {
    model: "qwen-audio-3.0-tts-flash",
    label: "Qwen-Audio 3.0 TTS Flash（12 个系统音色，快速）",
    family: "cosyvoice",
    transport: "websocket",
    systemVoices: ["longanfengyue", "longanhuan_v3.6"],
    supportsDesign: true,
    providers: ["bailian", "gateway"],
  },
] as unknown as TtsModelCapability[];

describe("Platform-specific model choices", () => {
  it("points every AgentScope preset at the proxy API root", () => {
    // The look-alikes are traps: /compatible-mode/v1 is not deployed on
    // platform-pre (it answers the frontend shell with a 200 HTML page), and
    // /api/v1 only half works. Either one yields a parse failure far from
    // its cause, and the media sections additionally pick their reference
    // transport from this URL.
    const seen: string[] = [];
    for (const [type, presets] of Object.entries(PRESETS_BY_TYPE)) {
      const preset = presets["AgentScope Platform"];
      if (!preset) continue;
      seen.push(type);
      expect(
        preset.base_url,
        `${type} preset must use the proxy API root`,
      ).toBe("https://platform.agentscope.io/v1");
    }
    // Sections measured to work on the proxy; a new one silently dropping
    // its preset would leave the dropdown pointing at Bailian.
    expect(seen.sort()).toEqual(["asr", "image", "tts", "video"]);
  });

  it("resolves an LLM endpoint for a protocol the host does not publish", () => {
    // The proxy is deliberately absent from the host provider catalog, so it
    // resolves to no provider id. The lookup used to give up there and leave
    // the field holding the URL of whatever provider had been selected
    // before, which sent an sk-as key off to Bailian.
    expect(llmPresetFor("AgentScope Platform", undefined, [])).toEqual({
      base_url: "https://platform.agentscope.io/v1",
      models: ["qwen3.8-flash", "qwen3.8-max"],
    });

    // A provider the host does publish still wins over the static table,
    // because its endpoint is the one the host holds the key for.
    const dashscope: HostProviderInfo = {
      id: "dashscope",
      name: "DashScope",
      base_url: "https://host.example/v1",
      freeze_url: false,
      models: [{ id: "qwen3.8-flash", name: "Qwen3.8 Flash" }],
      extra_models: [],
    };
    expect(llmPresetFor("DashScope（百炼）", "dashscope", [dashscope])).toEqual(
      {
        base_url: "https://host.example/v1",
        models: ["qwen3.8-flash"],
        base_url_options: undefined,
      },
    );

    // A protocol with no endpoint of its own offers nothing, so the field is
    // left as the user set it rather than being filled with a guess.
    expect(llmPresetFor("自定义", undefined, [])).toBeNull();
  });

  it("offers only the speech models the selected endpoint actually routes", () => {
    // The proxy publishes its own speech model list and answers anything
    // outside it with MODEL_NOT_ALLOWED, so a Bailian-only name in this
    // dropdown is a guaranteed failure the user has to discover by hand.
    const models = platformSpeechModels;
    expect(
      ttsModelChoices("AgentScope Platform", models).map((item) => item.model),
    ).toEqual(["qwen-audio-3.0-tts-flash"]);
    expect(
      ttsModelChoices("DashScope（百炼）", models).map((item) => item.model),
    ).toEqual(["qwen3-tts-flash", "qwen-audio-3.0-tts-flash"]);
  });
});

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

const UPLOAD_ROUTE = "qwenpaw/ivb/uploads";

function accepted(data: Record<string, unknown>) {
  return { request_id: "req-1", data };
}

function bundleBlob(size = 10) {
  return new Blob([new Uint8Array(size)], { type: "application/zip" });
}

describe("interactive bundle upload", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the zip multipart to the platform with the console cookie", async () => {
    const { calls, fetchMock } = installMockFetch([
      {
        match: UPLOAD_ROUTE,
        response: {
          json: accepted({
            upload_id: "u-1",
            status: "received",
            progress_url: "https://platform/game?upload_id=u-1",
          }),
        },
      },
    ]);

    const result = await uploadInteractiveBundle({
      bundle: bundleBlob(),
      projectId: "project-abc",
      idempotencyKey: "key-1",
    });

    // Relative path: the endpoint only exists behind this deployment's
    // subdomain, and the browser carries the console cookie there.
    expect(IVB_UPLOAD_PATH).toBe("/api/v1/qwenpaw/ivb/uploads");
    expect(calls[0]).toMatchObject({ url: IVB_UPLOAD_PATH, method: "POST" });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      credentials: "include",
    });

    const body = calls[0].body as Record<string, unknown>;
    expect(body.project_id).toBe("project-abc");
    expect(body.file).toBeTruthy();

    // Idempotency-Key rides the header; Content-Type is deliberately left to
    // the browser so the multipart boundary survives. Headers are lowercased.
    expect(calls[0].headers["idempotency-key"]).toBe("key-1");
    expect(calls[0].headers["content-type"]).toBeUndefined();

    expect(result.upload_id).toBe("u-1");
    expect(result.progress_url).toContain("upload_id=u-1");
  });

  it("refuses an empty bundle before touching the network", async () => {
    const { calls } = installMockFetch([{ match: UPLOAD_ROUTE, response: {} }]);
    await expect(
      uploadInteractiveBundle({ bundle: new Blob(), projectId: "p" }),
    ).rejects.toThrow("为空");
    expect(calls).toHaveLength(0);
  });

  it("surfaces the platform status on a rejected upload", async () => {
    installMockFetch([
      {
        match: UPLOAD_ROUTE,
        response: { status: 400, ok: false, json: accepted({}) },
      },
    ]);
    await expect(
      uploadInteractiveBundle({ bundle: bundleBlob(), projectId: "p" }),
    ).rejects.toThrow("HTTP 400");
  });

  it("mints a fresh idempotency key each call", () => {
    expect(newUploadIdempotencyKey()).not.toBe(newUploadIdempotencyKey());
  });
});
