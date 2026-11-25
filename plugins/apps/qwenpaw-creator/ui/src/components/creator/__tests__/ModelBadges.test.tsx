import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import ModelBadges from "../ModelBadges";
import type { ModelConfigData } from "@/contracts/creator";
import { configuredModelConfig } from "@/test/agentFixtures";
import { installMockFetch } from "@/test/mockFetch";
import { useModelConfigStore } from "@/store/modelConfigStore";

function renderBadges(tts: Partial<ModelConfigData["tts"]> = {}) {
  const config = {
    ...configuredModelConfig,
    llm: {
      ...configuredModelConfig.llm,
      protocol: "DashScope（百炼）",
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    tts: { ...configuredModelConfig.tts, ...tts },
  };
  const configResponse = { json: config };
  const { calls } = installMockFetch([
    {
      match: "/models/config",
      method: "GET",
      response: configResponse,
    },
    {
      match: "/host-providers",
      response: { json: { providers: [] } },
    },
  ]);
  render(<ModelBadges />);
  return { configResponse, calls };
}

beforeEach(() => {
  useModelConfigStore.setState({ config: null });
});

describe("ModelBadges", () => {
  it("shows Grounding as configured when it reuses a configured LLM", async () => {
    renderBadges();
    fireEvent.focus(screen.getByRole("button", { name: "模型配置" }));
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
    fireEvent.mouseEnter(screen.getByRole("button", { name: "模型配置" }));
    expect(await screen.findByLabelText(label)).toHaveAttribute(
      "data-status",
      dataStatus,
    );
  });
});
