import { describe, expect, it } from "vitest";
import { installMockFetch } from "@/test/mockFetch";
import {
  createPromptProposal,
  getPromptSync,
  type PromptProposal,
  type PromptSyncState,
} from "../promptSync";

const scope = {
  projectId: "p1",
  timelineId: "timeline:main",
  elementId: "shot:one",
};
const shots = {
  order: ["shot:one"],
  items: {
    "shot:one": {
      shot_id: "shot:one",
      description: "女子看向背包，轻声说：钥匙在这里。",
      camera: "静止",
      framing: "近景",
      duration_seconds: 6,
    },
  },
};
const state: PromptSyncState = {
  status: "needs_update",
  baselineToken: "baseline",
  shots,
  storyboardPrompt: "分镜正文",
  videoPrompt: "视频正文",
  changedSources: ["currentPlan", "videoPrompt"],
  suggestedSource: "mixed",
};
const proposal: PromptProposal = {
  proposalId: "proposal-1",
  baselineToken: "baseline",
  source: "mixed",
  beforeShots: shots,
  shots,
  beforeStoryboardPrompt: "原分镜正文",
  beforeVideoPrompt: "原视频正文",
  storyboardPrompt: "分镜正文",
  videoPrompt: "视频正文",
};

describe("bidirectional prompt synchronization API contract", () => {
  it("retains the server's complete three-part baseline and mixed source", async () => {
    installMockFetch([{ match: "/prompt-sync", response: { json: state } }]);
    expect(await getPromptSync(scope)).toEqual(state);
  });

  it("sends an explicit mixed source and returns the three-part atomic proposal", async () => {
    const { calls } = installMockFetch([
      {
        match: "/prompt-proposals",
        method: "POST",
        response: { json: proposal },
      },
    ]);
    expect(await createPromptProposal(scope, "mixed")).toEqual(proposal);
    expect(calls[0].url).toContain(
      "/timelines/timeline%3Amain/elements/shot%3Aone/prompt-proposals",
    );
    expect(calls[0].body).toEqual({ source: "mixed" });
  });

  it.each([
    { shots: undefined },
    { storyboardPrompt: undefined },
    { changedSources: undefined },
    { suggestedSource: null },
    { changedSources: ["storyboardPrompt"], suggestedSource: "currentPlan" },
  ])(
    "rejects incomplete or contradictory synchronization authority %#",
    async (change) => {
      installMockFetch([
        { match: "/prompt-sync", response: { json: { ...state, ...change } } },
      ]);
      await expect(getPromptSync(scope)).rejects.toThrow(
        "could not be checked",
      );
    },
  );

  it.each(["shots", "beforeShots", "beforeVideoPrompt", "source"] as const)(
    "rejects a proposal missing %s rather than allowing partial review",
    async (field) => {
      installMockFetch([
        {
          match: "/prompt-proposals",
          method: "POST",
          response: { json: { ...proposal, [field]: undefined } },
        },
      ]);
      await expect(createPromptProposal(scope, "mixed")).rejects.toThrow(
        "preview is incomplete",
      );
    },
  );
});
