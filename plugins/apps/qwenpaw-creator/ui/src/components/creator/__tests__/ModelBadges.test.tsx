import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ModelBadges from "../ModelBadges";
import type { ModelConfigData } from "@/contracts/creator";
import { configuredModelConfig } from "@/test/agentFixtures";
import { installMockFetch } from "@/test/mockFetch";

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

describe("ModelBadges", () => {
  it("distinguishes incomplete enabled search from an explicitly disabled service", async () => {
    renderBadges(
      {},
      {
        llm: {
          ...configuredModelConfig.llm,
          protocol: "OpenAI 协议",
          base_url: "https://gateway.example.test/v1",
        },
        grounding: {
          ...configuredModelConfig.grounding,
          enabled: true,
          search_reuse_llm: true,
          tavily_api_key: "",
          serper_api_key: "",
        },
      },
    );
    expect(
      await screen.findByLabelText("Grounding：配置不完整"),
    ).toHaveAttribute("data-status", "incomplete");
    expect(
      screen.queryByLabelText("Grounding：已配置但未启用"),
    ).not.toBeInTheDocument();
  });

  it("keeps a complete but disabled search service marked as disabled", async () => {
    renderBadges(
      {},
      { grounding: { ...configuredModelConfig.grounding, enabled: false } },
    );
    expect(
      await screen.findByLabelText("Grounding：已配置但未启用"),
    ).toHaveAttribute("data-status", "off");
  });

  it("shows Grounding as configured when it reuses a configured LLM", async () => {
    renderBadges();
    expect(await screen.findByLabelText("Grounding：已配置")).toHaveAttribute(
      "data-status",
      "on",
    );
  });

  it.each<[string, Partial<ModelConfigData["tts"]>, string, string]>([
    [
      "configured when enabled with its own key",
      { enabled: true, api_key: "saved-secret", voice: "Cherry" },
      "语音合成模型：已配置",
      "on",
    ],
    [
      "configured but idle when saved yet disabled",
      {},
      "语音合成模型：已配置但未启用",
      "off",
    ],
  ])("marks TTS %s", async (_name, tts, label, dataStatus) => {
    renderBadges(tts);
    expect(await screen.findByLabelText(label)).toHaveAttribute(
      "data-status",
      dataStatus,
    );
  });
});
