import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  MotionGraphicDocument,
  ProjectSettingsDocument,
} from "@/contracts/creator";
import SubtitleStyleSection, {
  captionDocumentOwnedByStyle,
  subtitleAnimationFromDocument,
  subtitleStyleFromSettings,
} from "../SubtitleStyleSection";

function settings(
  overrides: Partial<ProjectSettingsDocument> = {},
): ProjectSettingsDocument {
  return {
    aspect_ratio: "9:16",
    resolution: "1080x1920",
    platform: "douyin",
    language: "zh",
    target_duration_seconds: 30,
    content_type: "short_video",
    ...overrides,
  } as ProjectSettingsDocument;
}

interface SectionProps {
  settings?: Partial<ProjectSettingsDocument>;
  elementIds?: string[];
  animation?: Parameters<typeof SubtitleStyleSection>[0]["animation"];
  motionOwned?: boolean;
  busy?: boolean;
}

function renderSection(props: SectionProps = {}) {
  const onApply = vi.fn();
  render(
    <SubtitleStyleSection
      settings={settings(props.settings)}
      elementIds={props.elementIds}
      animation={props.animation}
      motionOwned={props.motionOwned}
      busy={props.busy}
      onApply={onApply}
    />,
  );
  return onApply;
}

/** The last request handed to the endpoint, as the section built it. */
function lastRequest(onApply: ReturnType<typeof vi.fn>): Record<string, unknown> {
  return onApply.mock.calls.at(-1)?.[0] as Record<string, unknown>;
}

describe("SubtitleStyleSection", () => {
  it("names the style that is actually in effect", () => {
    render(
      <SubtitleStyleSection
        settings={settings({ subtitle_template: "variety_pop" })}
        onApply={vi.fn()}
      />,
    );
    expect(screen.getByText("当前生效：综艺花字")).toBeInTheDocument();
  });

  it("says smart recommend while the project never picked a pack", () => {
    renderSection();
    expect(screen.getByText("当前生效：智能推荐")).toBeInTheDocument();
  });

  it("sends a pack pick project-wide, even from one caption's detail", async () => {
    const user = userEvent.setup();
    const onApply = renderSection({ elementIds: ["cap-1"] });

    await user.click(
      screen.getByRole("button", { name: /综艺花字/ }),
    );

    // Scoping a pack to one caption would leave the rest describing the old
    // look, so the request must carry no element ids at all.
    expect(lastRequest(onApply)).toEqual({ subtitleTemplate: "variety_pop" });
  });

  it("clears the pack with an explicit null for smart recommend", async () => {
    const user = userEvent.setup();
    const onApply = renderSection({
      settings: { subtitle_template: "variety_pop" },
    });

    await user.click(screen.getByRole("button", { name: /智能推荐/ }));

    expect(lastRequest(onApply)).toEqual({ subtitleTemplate: null });
  });

  it("keeps a colour override project-wide too", async () => {
    const user = userEvent.setup();
    const onApply = renderSection({ elementIds: ["cap-1"] });

    await user.click(screen.getByLabelText("#FFE066"));

    expect(lastRequest(onApply)).toEqual({
      subtitleStyle: { color: "#FFE066", fontScale: null, strokeScale: null },
    });
  });

  it("addresses only the open caption when the motion changes", async () => {
    const user = userEvent.setup();
    const onApply = renderSection({ elementIds: ["cap-1"] });

    await user.click(screen.getByText("弹入"));

    expect(lastRequest(onApply)).toEqual({
      animation: { entrance: "pop" },
      elementIds: ["cap-1"],
    });
  });

  it("drops the element scope when the bar is not inside a detail view", async () => {
    const user = userEvent.setup();
    const onApply = renderSection();

    await user.click(screen.getByText("弹入"));

    expect(lastRequest(onApply)).toEqual({ animation: { entrance: "pop" } });
  });

  it("tells the user the motion picker only reaches this caption", () => {
    renderSection({ elementIds: ["cap-1"] });
    expect(
      screen.getByText("动效只改这条字幕；风格包与样式作用于全项目字幕"),
    ).toBeInTheDocument();
  });

  it("re-describes the captions on demand without touching settings", async () => {
    const user = userEvent.setup();
    const onApply = renderSection();

    await user.click(screen.getByRole("button", { name: /同步动效描述/ }));

    expect(lastRequest(onApply)).toEqual({ syncPrompts: true });
  });

  it("goes inert while a request is in flight", async () => {
    const user = userEvent.setup();
    const onApply = renderSection({ busy: true });

    await user.click(screen.getByText("弹入"));
    await user.click(screen.getByRole("button", { name: /同步动效描述/ }));

    expect(onApply).not.toHaveBeenCalled();
  });

  it("trades the motion picker for an explanation when the design owns the look", () => {
    renderSection({ motionOwned: false });

    expect(
      screen.getByText(
        "这条字幕的外观由其动效设计决定，风格包不会重写它；入场/出场/循环请在动效设计里调整",
      ),
    ).toBeInTheDocument();
    // A picker whose choice the route would refuse is worse than no picker.
    expect(screen.queryByText("弹入")).not.toBeInTheDocument();
  });

  it("keeps the picker for a caption that has no document yet", () => {
    // compose falls back to the pack there, so the pick does land.
    renderSection({ motionOwned: true });
    expect(screen.getByText("弹入")).toBeInTheDocument();
  });

  it("echoes the persisted overrides back into the controls", () => {
    renderSection({
      settings: {
        subtitle_template: "cinematic",
        subtitle_style: { color: "#7DD3FC", font_scale: 1.25, stroke_scale: 0 },
      },
      animation: { entrance: "slide", exit: "soft_fade", loop: true },
    });

    expect(screen.getByText("当前生效：电影叙事")).toBeInTheDocument();
    expect(screen.getByLabelText("#7DD3FC")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText("滑入")).toHaveAttribute("aria-pressed", "true");
    // The pack default hint disappears once the document states a motion.
    expect(
      screen.queryByText("未选择时跟随风格包默认动效"),
    ).not.toBeInTheDocument();
  });
});

describe("subtitleStyleFromSettings", () => {
  it("maps the persisted snake_case overrides onto the control shape", () => {
    expect(
      subtitleStyleFromSettings(
        settings({
          subtitle_style: { color: null, font_scale: 0.85, stroke_scale: null },
        }),
      ),
    ).toEqual({ color: null, fontScale: 0.85, strokeScale: null });
  });

  it("reports null for a project that never overrode anything", () => {
    expect(subtitleStyleFromSettings(settings())).toBeNull();
  });
});

describe("captionDocumentOwnedByStyle", () => {
  const doc = (
    overrides: Partial<MotionGraphicDocument>,
  ): MotionGraphicDocument =>
    ({
      format: "html_css",
      motif: "caption_card",
      template_version: 3,
      ...overrides,
    }) as MotionGraphicDocument;

  it("matches the backend rule for derivable captions", () => {
    expect(captionDocumentOwnedByStyle(doc({}))).toBe(true);
    // A legacy document this pipeline never stamped is not derived data.
    expect(captionDocumentOwnedByStyle(doc({ template_version: null }))).toBe(
      false,
    );
    expect(captionDocumentOwnedByStyle(doc({ motif: "custom" }))).toBe(false);
    expect(captionDocumentOwnedByStyle(doc({ format: "html_js" }))).toBe(false);
    expect(captionDocumentOwnedByStyle(undefined)).toBe(false);
  });
});

describe("subtitleAnimationFromDocument", () => {
  const caption = (
    overrides: Partial<MotionGraphicDocument>,
  ): MotionGraphicDocument =>
    ({
      format: "html_css",
      motif: "caption_card",
      fps: 25,
      loop: false,
      design_notes: "",
      ...overrides,
    }) as MotionGraphicDocument;

  it("reads the triple back out of the stamp the renderer wrote", () => {
    expect(
      subtitleAnimationFromDocument(
        caption({
          entrance: "slide",
          exit: "soft_fade",
          design_notes: "caption_style=variety_pop|None caption_animation=slide/soft_fade/1",
        }),
      ),
    ).toEqual({ entrance: "slide", exit: "soft_fade", loop: true });
  });

  it("will not light up a pill the renderer cannot play", () => {
    // A token outside the vocabulary can only come from a damaged or future
    // document; reading it back verbatim would show a choice the picker
    // cannot send and the compositor would ignore.
    expect(
      subtitleAnimationFromDocument(
        caption({ design_notes: "caption_animation=bounce/dissolve/1" }),
      ),
    ).toEqual({ entrance: null, exit: null, loop: true });
  });

  it("reports nothing for a document this pipeline never drew", () => {
    expect(
      subtitleAnimationFromDocument(caption({ design_notes: "hand authored" })),
    ).toBeNull();
    expect(subtitleAnimationFromDocument(undefined)).toBeNull();
  });

  it("stays quiet about documents the caption bar does not own", () => {
    expect(
      subtitleAnimationFromDocument(
        caption({ motif: "custom", design_notes: "caption_animation=pop/none/0" }),
      ),
    ).toBeNull();
    expect(
      subtitleAnimationFromDocument(
        caption({ format: "html_js", design_notes: "caption_animation=pop/none/0" }),
      ),
    ).toBeNull();
  });
});
