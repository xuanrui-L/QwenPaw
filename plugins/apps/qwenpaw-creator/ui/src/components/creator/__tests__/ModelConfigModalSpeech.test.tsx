import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ModelConfigModal from "@/components/creator/ModelConfigModal";
import { installMockFetch } from "@/test/mockFetch";
import type { ModelConfigData } from "@/contracts/creator";

/**
 * The speech section must describe only what the selected model can do.
 * Offering a system-voice picker for a model that has none, or asking the user
 * to name the clone/design companion models, turns a working configuration
 * into a failing one at synthesis time.
 */

const baseConfig: ModelConfigData = {
  llm: {
    enabled: true,
    model_name: "qwen3.7-plus",
    api_key: "saved-secret",
    base_url: "https://example.test/v1",
    protocol: "OpenAI 协议",
    custom_protocol: "",
    multimodal: true,
  },
  vlm: {
    enabled: true,
    model_name: "qwen3.7-plus",
    api_key: "saved-secret",
    base_url: "https://example.test/v1",
    protocol: "OpenAI 协议",
    custom_protocol: "",
    use_llm: true,
    multimodal: true,
  },
  grounding: {
    enabled: false,
    model_name: "",
    api_key: "",
    base_url: "",
    protocol: "OpenAI 协议",
    custom_protocol: "",
    reuse_llm: true,
    validation_source: "llm",
    tavily_api_key: "",
    native_search_enabled: true,
    search_provider: "dashscope_qwen",
    search_reuse_llm: true,
    search_model_name: "",
    search_api_key: "",
    search_base_url: "",
    search_protocol: "DashScope（百炼）",
  },
  asr: {
    enabled: false,
    model_name: "fun-asr",
    api_key: "",
    base_url: "https://dashscope.aliyuncs.com/api/v1",
    protocol: "DashScope Fun-ASR",
    custom_protocol: "",
    provider: "fun-asr",
    language: "",
    reuse_llm_key: true,
  },
  tts: {
    enabled: true,
    model_name: "qwen3-tts-flash",
    api_key: "",
    base_url: "https://dashscope.aliyuncs.com/api/v1",
    protocol: "DashScope（百炼）",
    custom_protocol: "",
    voice: "Cherry",
    vc_model_name: "",
    reuse_llm_key: true,
  },
  image: {
    enabled: false,
    model_name: "",
    api_key: "",
    base_url: "",
    protocol: "OpenAI 协议",
    custom_protocol: "",
  },
  video: {
    enabled: false,
    model_name: "",
    api_key: "",
    base_url: "",
    protocol: "DashScope（百炼）",
    custom_protocol: "",
  },
  oss: {
    enabled: false,
    access_key_id: "",
    access_key_secret: "",
    endpoint: "",
    bucket: "",
    public_base_url: "",
    policy_api_key: "",
  },
  executionAuthorization: { mode: "allow_all" },
};

const capabilities = {
  default: "qwen3-tts-flash",
  models: [
    {
      model: "qwen3-tts-flash",
      label: "Qwen3 TTS Flash（系统音色，快速）",
      family: "qwen-tts",
      transport: "http",
      systemVoices: ["Cherry", "Ethan"],
      supportsDesign: true,
    },
    {
      model: "cosyvoice-v3.5-plus",
      label: "CosyVoice 3.5 Plus（无系统音色，需先设计或复刻音色）",
      family: "cosyvoice",
      transport: "websocket",
      systemVoices: [],
      supportsDesign: true,
    },
  ],
};

function mount(config: ModelConfigData = baseConfig) {
  installMockFetch([
    { match: "/models/tts-capabilities", method: "GET", response: { json: capabilities } },
    { match: "/models/config", method: "GET", response: { json: config } },
    { match: "/host-providers", method: "GET", response: { json: { providers: [] } } },
  ]);
  render(<ModelConfigModal open onClose={() => {}} />);
}

async function openSpeechCard() {
  // The collapsed card header carries the label plus the current model name.
  const headers = await screen.findAllByText(/TTS 语音合成/);
  const card = headers
    .map((node) => node.closest(".glass-card") ?? node.parentElement)
    .find((node): node is HTMLElement => node instanceof HTMLElement);
  fireEvent.click(card as HTMLElement);
}

describe("ModelConfigModal speech section", () => {
  it("never asks the user to name the clone or design companion model", async () => {
    mount();
    await openSpeechCard();
    await waitFor(() => {
      expect(screen.getByText("默认旁白音色")).toBeInTheDocument();
    });
    expect(screen.queryByText(/声音复刻模型/)).not.toBeInTheDocument();
    expect(
      screen.getByText(/复刻\/设计所用的配套模型由后端自动选择/),
    ).toBeInTheDocument();
  });

  it("offers only the system voices the selected model actually has", async () => {
    mount();
    await openSpeechCard();
    await waitFor(() => {
      expect(screen.getByText("默认旁白音色")).toBeInTheDocument();
    });
    const voiceInput = screen
      .getByText("默认旁白音色")
      .parentElement?.querySelector("input");
    expect(voiceInput).toHaveValue("Cherry");
  });

  it("hides the voice picker for a model without system voices", async () => {
    mount({
      ...baseConfig,
      tts: { ...baseConfig.tts, model_name: "cosyvoice-v3.5-plus", voice: "" },
    });
    await openSpeechCard();
    await waitFor(() => {
      expect(
        screen.getByText(/该模型没有系统音色：Agent 会先根据角色设定设计专属音色/),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("默认旁白音色")).not.toBeInTheDocument();
  });
});
