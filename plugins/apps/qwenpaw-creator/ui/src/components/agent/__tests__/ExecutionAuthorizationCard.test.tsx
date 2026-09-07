import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ExecutionAuthorizationCard, {
  authorizationApprovalPayload,
  authorizationCheckpointPhase,
  authorizationDetail,
  authorizationJumpTarget,
  authorizationModelLabel,
  authorizationOperation,
  authorizationParameterSummary,
} from "@/components/agent/ExecutionAuthorizationCard";
import { navigateToLocator } from "@/routing/locators";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { useTimelineStore } from "@/store/timelineStore";
import { useOnboardingStore } from "@/store/onboardingStore";
import { makePendingAuthorization } from "@/test/agentFixtures";
import { projectDocument } from "@/test/creatorFixtures";

vi.mock("@/routing/locators", () => ({ navigateToLocator: vi.fn() }));

describe("execution authorization public presentation", () => {
  beforeEach(() => {
    useExecutionAuthorizationStore.getState().reset();
    useExecutionAuthorizationStore.getState().bindProject("p1");
    useTimelineStore.setState({ activeTimelineId: "timeline:main" });
    useOnboardingStore.setState({ hints: {} });
  });

  it.each([
    ["design", "设计确认", "assets"],
    ["structure", "结构确认", "blueprint"],
    ["script", "剧本确认", "blueprint"],
    ["direction", "方向确认", "blueprint"],
  ])(
    "presents %s checkpoint as creative confirmation with a real view destination",
    (phase, label, page) => {
      const authorization = makePendingAuthorization({
        targetRef: "project:p1",
        provider: "creator-checkpoint",
        model: label,
        scope: {
          operation: `creation_checkpoint_${phase}`,
          checkpointPhase: phase,
          message: "private checkpoint instructions",
        },
      });
      const { container } = render(
        <ExecutionAuthorizationCard authorization={authorization} />,
      );
      expect(container.textContent).toContain(`${label}等待确认`);
      expect(container.textContent).not.toMatch(
        /creator-checkpoint|高成本|付费|生产确认|private checkpoint|模型/,
      );
      expect(
        container.querySelector(
          '[data-onboarding-hint="executionAuthorization"]',
        ),
      ).toBeNull();
      expect(authorizationModelLabel(authorization)).toBe("");
      expect(authorizationDetail(authorization)).toBe(`${label} · 当前项目`);
      fireEvent.click(screen.getByRole("button", { name: "查看" }));
      expect(navigateToLocator).toHaveBeenCalledWith(
        "p1",
        { page },
        expect.objectContaining({ review: true, description: label }),
      );
      // The approval API still requires the original transport values.
      expect(authorizationApprovalPayload(authorization)).toMatchObject({
        provider: "creator-checkpoint",
        model: label,
        authorizationToken: "token-1",
      });
    },
  );

  it("recognizes the existing checkpointPhase only for the checkpoint operation", () => {
    expect(
      authorizationCheckpointPhase(
        makePendingAuthorization({
          scope: {
            operation: "creation_checkpoint",
            checkpointPhase: "design",
          },
        }),
      ),
    ).toBe("design");
    expect(
      authorizationCheckpointPhase(
        makePendingAuthorization({
          scope: { operation: "image_generation", checkpointPhase: "design" },
        }),
      ),
    ).toBeNull();
    const unknown = makePendingAuthorization({
      provider: "creator-checkpoint",
      model: "private-future-checkpoint",
      scope: {
        operation: "creation_checkpoint_private-future",
        checkpointPhase: "plan",
      },
    });
    expect(authorizationOperation(unknown)).toBe("创作确认");
    expect(authorizationModelLabel(unknown)).toBe("");
    expect(authorizationJumpTarget(unknown)).toBeNull();
  });

  it("never renders scope copy, unknown operations, raw references or model diagnostics", () => {
    const authorization = makePendingAuthorization({
      targetRef: "unrecognized:private-target-901",
      provider: 'provider returned {"api_key":"private-key"}',
      model: "/private/weights/model.bin",
      scope: {
        operation: "internal_dispatch_machine_action",
        message: 'thinking: private-thought {"action":"private-action"}',
        parameters: {
          durationSeconds: { prompt: "private-duration" },
          resolution: "private-resolution",
          ratio: "private-ratio",
          aspectRatio: "private-aspect",
          generateAudio: "private-audio",
        },
      },
    });
    const { container } = render(
      <ExecutionAuthorizationCard authorization={authorization} />,
    );
    expect(container.textContent).not.toMatch(
      /private-|internal_dispatch|thinking|api_key/,
    );
    expect(container.textContent).toContain("当前项目");
    expect(screen.queryByText("参数")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "查看" }),
    ).not.toBeInTheDocument();
    expect(authorizationDetail(authorization)).not.toMatch(
      /private-|internal_dispatch/,
    );
  });

  it("keeps supported operations, public object names and billing model identity", () => {
    const authorization = makePendingAuthorization({
      targetRef: "visual-entity:cat",
      model: "qwen-image-2.0-pro",
      scope: {
        operation: "image_generation",
        message: "private prompt must not appear",
        parameters: { aspectRatio: "16:9", resolution: "1080p" },
      },
    });
    const detail = authorizationDetail(authorization, projectDocument);
    expect(detail).toContain("圆润大橘猫");
    expect(detail).toContain("dashscope / qwen-image-2.0-pro");
    expect(detail).toContain("画幅 16:9");
    expect(detail).not.toMatch(/visual-entity:|private prompt/);
    expect(
      authorizationOperation(
        makePendingAuthorization({ scope: { operation: "tts_generation" } }),
      ),
    ).toBe("合成语音");
  });

  it("only summarizes finite positive durations, formatted dimensions and boolean audio", () => {
    expect(
      authorizationParameterSummary(
        makePendingAuthorization({
          scope: {
            parameters: {
              durationSeconds: 3.5,
              resolution: "720p",
              ratio: "16:9",
              aspectRatio: "9:16",
              generateAudio: false,
            },
          },
        }),
      ),
    ).toBe("时长 3.5秒 · 分辨率 720P · 比例 16:9 · 画幅 9:16 · 无声");
    expect(
      authorizationParameterSummary(
        makePendingAuthorization({
          scope: {
            parameters: { generateAudio: true },
          },
        }),
      ),
    ).toBe("有声");
  });

  it.each([
    null,
    [],
    { durationSeconds: "5" },
    { durationSeconds: true },
    { durationSeconds: 0 },
    { durationSeconds: -1 },
    { durationSeconds: Number.NaN },
    { durationSeconds: Number.POSITIVE_INFINITY },
    {
      ratio: "0:9",
      aspectRatio: "16:0",
      resolution: "../../private",
      generateAudio: "true",
    },
    {
      ratio: "16:9\ninternal",
      aspectRatio: "[16,9]",
      resolution: "720p\ninternal",
    },
  ])("omits malformed parameter values: %j", (parameters) => {
    expect(
      authorizationParameterSummary(
        makePendingAuthorization({ scope: { parameters } }),
      ),
    ).toBe("");
  });

  it("does not replace the approval contract with the sanitized display identity", () => {
    const authorization = makePendingAuthorization({
      provider: "custom provider",
      model: "model/error details",
      maxCandidates: 2,
    });
    expect(authorizationModelLabel(authorization)).not.toContain(
      "custom provider",
    );
    expect(authorizationApprovalPayload(authorization)).toEqual({
      authorizationToken: "token-1",
      provider: "custom provider",
      model: "model/error details",
      maxCost: 0,
      maxCandidates: 2,
    });
  });

  it("keeps the view action pointed at the video prompt for video authorization", () => {
    const authorization = makePendingAuthorization({
      targetRef: "element:r2v-window",
      scope: { operation: "r2v_generation" },
    });
    const target = authorizationJumpTarget(authorization, projectDocument);
    expect(target?.field).toMatch(/\/creation\/video_prompt$/);
    render(
      <ExecutionAuthorizationCard
        authorization={authorization}
        project={projectDocument}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "查看" }));
    expect(navigateToLocator).toHaveBeenCalledWith(
      "p1",
      target?.locator,
      expect.objectContaining({
        review: true,
        field: target?.field,
      }),
    );
  });

  it.each(["visual-entity", "visual-variant"])(
    "locates the exact variant prompt for %s authorization",
    (prefix) => {
      const target = authorizationJumpTarget(
        makePendingAuthorization({
          targetRef: `${prefix}:cat@variant:cat:default`,
          scope: { operation: "image_generation" },
        }),
        projectDocument,
      );
      expect(target).toEqual({
        locator: {
          page: "assets",
          assetId: "cat",
          variantId: "variant:cat:default",
          field:
            "/visual/entities/items/cat/variants/items/variant:cat:default/prompt",
        },
        field:
          "/visual/entities/items/cat/variants/items/variant:cat:default/prompt",
      });
    },
  );

  it("uses the authorized element's real timeline instead of the active timeline", () => {
    const project = structuredClone(projectDocument);
    project.timelines.items["timeline:ep2"].elements_by_id["second-shot"] = {
      ...project.timelines.items["timeline:main"].elements_by_id["r2v-window"],
      element_id: "second-shot",
    };
    const target = authorizationJumpTarget(
      makePendingAuthorization({
        targetRef: "element:second-shot",
        scope: { operation: "r2v_generation" },
      }),
      project,
    );
    expect(target?.locator).toEqual({
      page: "element",
      elementId: "second-shot",
      timelineId: "timeline:ep2",
      field:
        "/timelines/items/timeline:ep2/elements_by_id/second-shot/creation/video_prompt",
    });
    expect(target?.field).toBe(
      "/timelines/items/timeline:ep2/elements_by_id/second-shot/creation/video_prompt",
    );
  });

  it("uses the real image authorization parameters.variantId instead of choosing the active variant", () => {
    const project = structuredClone(projectDocument);
    const entity = project.visual.entities.items.cat;
    entity.variants.items["variant:default"] = {
      ...entity.variants.items["variant:cat:default"],
      variant_id: "variant:default",
      selected_artifact_version_id: null,
      generated_artifact_version_ids: [],
    };
    entity.variants.order.push("variant:default");
    const authorization = makePendingAuthorization({
      targetRef: "asset:cat",
      scope: {
        operation: "image_generation",
        parameters: { aspectRatio: "9:16", variantId: "variant:default" },
      },
    });
    expect(authorizationJumpTarget(authorization, project)).toEqual({
      locator: {
        page: "assets",
        assetId: "cat",
        variantId: "variant:default",
        field:
          "/visual/entities/items/cat/variants/items/variant:default/prompt",
      },
      field: "/visual/entities/items/cat/variants/items/variant:default/prompt",
    });
    expect(authorizationParameterSummary(authorization)).toBe("画幅 9:16");
    expect(
      authorizationJumpTarget(
        {
          ...authorization,
          targetRef: "visual-variant:cat@variant:cat:default",
        },
        project,
      )?.locator.variantId,
    ).toBe("variant:cat:default");
    expect(authorizationJumpTarget(authorization, projectDocument)).toBeNull();
  });

  it.each(["继续", "取消"])(
    "keeps %s available after an error without exposing provider details",
    async (label) => {
      const method = label === "继续" ? "approve" : "decline";
      const action = vi
        .spyOn(useExecutionAuthorizationStore.getState(), method)
        .mockRejectedValue(
          new Error('private-provider-error {"requestId":"private-id"}'),
        );
      const errorToast = vi.spyOn(message, "error");
      render(
        <ExecutionAuthorizationCard
          authorization={makePendingAuthorization()}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() =>
        expect(errorToast).toHaveBeenCalledWith("执行失败，请重试"),
      );
      expect(action).toHaveBeenCalled();
      expect(screen.getByRole("button", { name: label })).toBeEnabled();
      expect(document.body.textContent).not.toContain("private-provider-error");
    },
  );
});
