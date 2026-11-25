import type {
  SubtitleAnimationPolicy,
  SubtitleStyle,
} from "@/contracts/creator";
import SubtitleAnimationControls from "../SubtitleAnimationControls";
import SubtitleStyleControls from "../SubtitleStyleControls";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type {
  MotionGraphicDocument,
  ProjectSettingsDocument,
} from "@/contracts/creator";
import SubtitleStyleSection, {
  captionDocumentOwnedByStyle,
  subtitleAnimationFromDocument,
  type SubtitleStyleDraft,
} from "../SubtitleStyleSection";
import {
  subtitleStyleBaseFrom,
  diffSubtitleStyleDraft,
  subtitleStyleDraftDirtyCount,
  subtitleStyleDraftRequests,
  subtitleStyleFromSettings,
} from "../subtitleStyleDraft";

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
  /** What the caption document plays today; ``undefined`` = pack default. */
  animation?: SubtitleStyleDraft["animation"];
  motionOwned?: boolean;
  elementScoped?: boolean;
  busy?: boolean;
  /** The bar hides the motion picker unless a test asks for it back. */
  showMotion?: boolean;
}

/**
 * The bar is a controlled draft, so the test needs the owner the detail panel
 * is: state that the pickers write into.  The bar no longer holds a write
 * path at all — only the panel's 「应用修改」 sends the diff — so the tests
 * read the draft and the 「当前生效」 line, never a request.
 */
function renderSection(props: SectionProps = {}) {
  const live = subtitleStyleBaseFrom(
    settings(props.settings),
    props.animation ?? null,
  );
  let latestDraft: SubtitleStyleDraft = live;

  function Harness() {
    const [draft, setDraft] = useState<SubtitleStyleDraft>(live);
    return (
      <SubtitleStyleSection
        settings={settings(props.settings)}
        base={live}
        draft={draft}
        onDraftChange={(next) => {
          latestDraft = next;
          setDraft(next);
        }}
        motionOwned={props.motionOwned}
        elementScoped={props.elementScoped}
        busy={props.busy}
        showMotion={props.showMotion}
      />
    );
  }

  render(<Harness />);
  return {
    draft: () => latestDraft,
  };
}

describe("SubtitleStyleSection", () => {
  it("names the style that is actually in effect", () => {
    render(
      <SubtitleStyleSection
        settings={settings({ subtitle_template: "variety_pop" })}
        base={subtitleStyleBaseFrom(
          settings({ subtitle_template: "variety_pop" }),
          null,
        )}
        draft={subtitleStyleBaseFrom(
          settings({ subtitle_template: "variety_pop" }),
          null,
        )}
        onDraftChange={vi.fn()}
      />,
    );
    expect(screen.getByText("当前生效：综艺花字")).toBeInTheDocument();
  });

  it("keeps a pack pick local instead of rewriting every caption at once", async () => {
    const user = userEvent.setup();
    const view = renderSection();

    await user.click(screen.getByRole("button", { name: /综艺花字/ }));

    // The old behaviour: one click PATCHed the project, redrew every caption
    // document and invalidated the composed master. A browse must be free,
    // and the bar no longer even holds a write path to do it with.
    expect(view.draft()).toMatchObject({ packId: "variety_pop" });
    // And the bar still tells the truth about what is playing right now.
    expect(screen.getByText("当前生效：智能推荐")).toBeInTheDocument();
  });

  it("counts the pending groups so the confirm button can carry them", async () => {
    const user = userEvent.setup();
    renderSection({ settings: { subtitle_template: "variety_pop" } });

    expect(screen.queryByText(/字幕风格改动待确认/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /电影叙事/ }));
    await user.click(screen.getByLabelText("#FFE066"));

    const pending = await waitFor(() => screen.getByText(/字幕风格改动待确认/));
    expect(pending.textContent).toContain("2");
  });

  it("drops the pending count back to nothing when the pick is undone", async () => {
    const user = userEvent.setup();
    renderSection({ settings: { subtitle_template: "variety_pop" } });

    await user.click(screen.getByRole("button", { name: /电影叙事/ }));
    expect(screen.getByText(/字幕风格改动待确认/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /综艺花字/ }));
    expect(screen.queryByText(/字幕风格改动待确认/)).not.toBeInTheDocument();
  });

  it("keeps a colour override in the draft too", async () => {
    const user = userEvent.setup();
    const view = renderSection();

    await user.click(screen.getByLabelText("#FFE066"));

    expect(view.draft().style).toEqual({
      color: "#FFE066",
      fontFamily: null,
      fontScale: null,
      strokeScale: null,
      backgroundOpacity: null,
    });
  });

  it("holds a motion pick for the confirm button as well", async () => {
    const user = userEvent.setup();
    const view = renderSection({ elementScoped: true, showMotion: true });

    await user.click(screen.getByText("弹入"));

    expect(view.draft().animation).toEqual({ entrance: "pop" });
  });

  it("echoes the draft back into the pills, not the persisted setting", async () => {
    const user = userEvent.setup();
    renderSection({
      settings: {
        subtitle_template: "cinematic",
        subtitle_style: { color: "#7DD3FC", font_scale: 1.25, stroke_scale: 0 },
      },
      animation: { entrance: "slide", exit: "soft_fade", loop: true },
      showMotion: true,
    });

    expect(screen.getByLabelText("#7DD3FC")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText("滑入")).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByText("弹入"));
    expect(screen.getByText("弹入")).toHaveAttribute("aria-pressed", "true");
  });

  it("offers no button of its own to write the picks", () => {
    // The 「按所选风格同步动效描述」 button is gone: the panel's 应用修改
    // already syncs descriptions and redraws documents on every pack or style
    // switch (the route's syncPrompts defaults to true), so a second button
    // only differed by demanding no change at all — and two write buttons in
    // one panel is how a browse stops being free.
    renderSection();
    expect(
      screen.queryByRole("button", { name: /同步动效描述/ }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /应用修改/ }),
    ).not.toBeInTheDocument();
  });

  it("goes inert while a request is in flight", async () => {
    const user = userEvent.setup();
    const view = renderSection({ busy: true, showMotion: true });

    await user.click(screen.getByText("弹入"));

    expect(view.draft().animation).toBeNull();
  });

  it("hides the motion picker when the authored design owns the look", () => {
    renderSection({ motionOwned: false, showMotion: true });

    expect(screen.queryByText("弹入")).not.toBeInTheDocument();
  });

  it("keeps the picker for a caption that has no document yet", () => {
    // compose falls back to the pack there, so the pick does land.
    renderSection({ motionOwned: true, showMotion: true });
    expect(screen.getByText("弹入")).toBeInTheDocument();
  });

  it("offers no motion picker while the renderer is offline", () => {
    // The default is the shipped state: an entrance pill the composed master
    // cannot honour is the "my setting did nothing" report, so the picker
    // stays out of the bar until someone asks for it back.
    renderSection();
    expect(screen.queryByText("字幕动效")).not.toBeInTheDocument();
    expect(screen.queryByText("弹入")).not.toBeInTheDocument();
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
          design_notes:
            "caption_style=variety_pop|None caption_animation=slide/soft_fade/1",
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
        caption({
          motif: "custom",
          design_notes: "caption_animation=pop/none/0",
        }),
      ),
    ).toBeNull();
    expect(
      subtitleAnimationFromDocument(
        caption({
          format: "html_js",
          design_notes: "caption_animation=pop/none/0",
        }),
      ),
    ).toBeNull();
  });
});

const base = (
  settingsOverrides: Partial<ProjectSettingsDocument> = {},
  animation = null,
) => subtitleStyleBaseFrom(settings(settingsOverrides), animation);

describe("subtitleStyleBaseFrom", () => {
  it("starts from what the project actually plays", () => {
    expect(
      base({
        subtitle_template: "variety_pop",
        subtitle_style: {
          color: "#FFE066",
          font_scale: null,
          stroke_scale: 0,
          background_opacity: 0.3,
        },
      }),
    ).toEqual({
      packId: "variety_pop",
      style: {
        color: "#FFE066",
        fontFamily: null,
        fontScale: null,
        strokeScale: 0,
        backgroundOpacity: 0.3,
      },
      animation: null,
    });
  });

  it("reads an untouched project as no overrides at all", () => {
    const live = base();
    expect(live.packId).toBeNull();
    expect(subtitleStyleFromSettings(settings())).toBeNull();
  });
});

describe("diffSubtitleStyleDraft", () => {
  it("sees nothing pending while the draft equals the live style", () => {
    const live = base({ subtitle_template: "variety_pop" });
    expect(diffSubtitleStyleDraft({ ...live }, live)).toEqual({
      pack: false,
      style: false,
      animation: false,
    });
    expect(subtitleStyleDraftDirtyCount({ ...live }, live)).toBe(0);
  });

  it("counts each group once, however many pills it moved", () => {
    const live = base();
    const draft = {
      packId: "cinematic",
      style: { color: "#FFE066", fontScale: 1.25, strokeScale: null },
      animation: { entrance: "pop" as const, loop: true },
    };
    expect(subtitleStyleDraftDirtyCount(draft, live)).toBe(3);
  });

  it("treats an all-default override object as no override", () => {
    const live = base({ subtitle_style: { color: "#FFE066" } });
    expect(diffSubtitleStyleDraft({ ...live, style: null }, live).style).toBe(
      true,
    );
    const clean = base();
    expect(
      diffSubtitleStyleDraft(
        {
          ...clean,
          style: {
            color: null,
            fontScale: null,
            strokeScale: null,
            backgroundOpacity: null,
          },
        },
        clean,
      ).style,
    ).toBe(false);
  });

  it("sees a plate-opacity pick as a style change, 跟随风格 as none", () => {
    // 0 is a stated wish (drop the block), so it has to be pending; leaving
    // the field out means "the pack's own alpha" and never is.
    const live = base({ subtitle_template: "clean_talk" });
    expect(
      diffSubtitleStyleDraft({ ...live, style: { backgroundOpacity: 0 } }, live)
        .style,
    ).toBe(true);
    expect(diffSubtitleStyleDraft({ ...live, style: {} }, live).style).toBe(
      false,
    );
    expect(
      subtitleStyleDraftRequests(
        { ...live, style: { backgroundOpacity: 0.5 } },
        live,
      ),
    ).toEqual([{ subtitleStyle: { backgroundOpacity: 0.5 } }]);
  });

  it("counts going back to smart recommend as a change", () => {
    const live = base({ subtitle_template: "cinematic" });
    expect(diffSubtitleStyleDraft({ ...live, packId: null }, live).pack).toBe(
      true,
    );
  });

  it("never reads an unset motion field as a change", () => {
    // "Follow the style pack" has no token to send, so a draft that leaves a
    // field out cannot be pending on it.
    const live = base({}, { entrance: "pop", exit: "none", loop: false });
    expect(
      diffSubtitleStyleDraft({ ...live, animation: { loop: false } }, live)
        .animation,
    ).toBe(false);
    expect(
      diffSubtitleStyleDraft({ ...live, animation: { loop: true } }, live)
        .animation,
    ).toBe(true);
  });

  it("goes clean once a partial motion pick has landed", () => {
    // The renderer stamps every field it resolved, so the live triple after a
    // successful pick is fuller than the override that was sent; comparing
    // them field by field would keep the confirm button alive forever.
    const before = base({ subtitle_template: "variety_pop" }, null);
    const draft = { ...before, animation: { loop: true } };
    expect(diffSubtitleStyleDraft(draft, before).animation).toBe(true);

    const after = base(
      { subtitle_template: "variety_pop" },
      { entrance: "pop", exit: "none", loop: true },
    );
    expect(diffSubtitleStyleDraft(draft, after).animation).toBe(false);
    expect(subtitleStyleDraftRequests(draft, after, ["cap-1"])).toEqual([]);
  });
});

describe("subtitleStyleDraftRequests", () => {
  it("sends nothing for a draft that only repeats the live style", () => {
    const live = base({ subtitle_template: "cinematic" });
    expect(subtitleStyleDraftRequests({ ...live }, live, ["cap-1"])).toEqual(
      [],
    );
  });

  it("keeps a pack pick project-wide, with no element ids", () => {
    const live = base();
    expect(
      subtitleStyleDraftRequests({ ...live, packId: "variety_pop" }, live, [
        "cap-1",
      ]),
    ).toEqual([{ subtitleTemplate: "variety_pop" }]);
  });

  it("clears the pack with an explicit null", () => {
    const live = base({ subtitle_template: "variety_pop" });
    expect(subtitleStyleDraftRequests({ ...live, packId: null }, live)).toEqual(
      [{ subtitleTemplate: null }],
    );
  });

  it("merges pack and overrides into one project-wide call", () => {
    const live = base();
    expect(
      subtitleStyleDraftRequests(
        { ...live, packId: "cinematic", style: { color: "#FFE066" } },
        live,
        ["cap-1"],
      ),
    ).toEqual([
      {
        subtitleTemplate: "cinematic",
        subtitleStyle: { color: "#FFE066" },
      },
    ]);
  });

  it("addresses only the open caption with a motion pick", () => {
    const live = base();
    expect(
      subtitleStyleDraftRequests(
        { ...live, animation: { entrance: "pop" } },
        live,
        ["cap-1"],
      ),
    ).toEqual([{ animation: { entrance: "pop" }, elementIds: ["cap-1"] }]);
    // Outside a detail view the same pick reaches every subtitle.
    expect(
      subtitleStyleDraftRequests(
        { ...live, animation: { entrance: "pop" } },
        live,
      ),
    ).toEqual([{ animation: { entrance: "pop" } }]);
  });

  it("splits a mixed draft into two calls, pack first", () => {
    // The route rejects a pack addressed at single captions, and the motion
    // override only makes sense after the pack redrew the documents.
    const live = base();
    expect(
      subtitleStyleDraftRequests(
        { ...live, packId: "variety_pop", animation: { entrance: "slide" } },
        live,
        ["cap-1"],
      ),
    ).toEqual([
      { subtitleTemplate: "variety_pop" },
      { animation: { entrance: "slide" }, elementIds: ["cap-1"] },
    ]);
  });
});

function AnimationHarness({
  initial = null,
  allowClear = false,
  onEmit,
}: {
  initial?: SubtitleAnimationPolicy | null;
  allowClear?: boolean;
  onEmit?: (animation: SubtitleAnimationPolicy | null) => void;
}) {
  const [value, setValue] = useState<SubtitleAnimationPolicy | null>(initial);
  return (
    <SubtitleAnimationControls
      value={value}
      allowClear={allowClear}
      onChange={(next) => {
        setValue(next);
        onEmit?.(next);
      }}
    />
  );
}

describe("SubtitleAnimationControls", () => {
  it("emits one picked entrance without inventing the other choices", async () => {
    const user = userEvent.setup();
    const emitted: (SubtitleAnimationPolicy | null)[] = [];
    render(
      <AnimationHarness onEmit={(animation) => emitted.push(animation)} />,
    );

    await user.click(screen.getByText("弹入"));

    expect(emitted.at(-1)).toEqual({ entrance: "pop" });
    expect(screen.getByText("弹入")).toHaveAttribute("aria-pressed", "true");
  });

  it("accumulates the triple across picks", async () => {
    const user = userEvent.setup();
    const emitted: (SubtitleAnimationPolicy | null)[] = [];
    render(
      <AnimationHarness onEmit={(animation) => emitted.push(animation)} />,
    );

    await user.click(screen.getByText("滑入"));
    await user.click(screen.getByText("缩小"));
    await user.click(screen.getByText("开"));

    expect(emitted.at(-1)).toEqual({
      entrance: "slide",
      exit: "shrink",
      loop: true,
    });
  });

  it("reflects what the caption document already plays", () => {
    render(
      <AnimationHarness
        initial={{ entrance: "type_on", exit: "none", loop: false }}
      />,
    );
    expect(screen.getByText("逐字浮现")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      screen.queryByText("未选择时跟随风格包默认动效"),
    ).not.toBeInTheDocument();
  });

  it("takes a pick back on a second click when clearing is allowed", async () => {
    const user = userEvent.setup();
    const emitted: (SubtitleAnimationPolicy | null)[] = [];
    render(
      <AnimationHarness
        allowClear
        onEmit={(animation) => emitted.push(animation)}
      />,
    );

    await user.click(screen.getByText("弹入"));
    expect(emitted.at(-1)).toEqual({ entrance: "pop" });

    await user.click(screen.getByText("弹入"));
    expect(emitted.at(-1)).toBeNull();
    expect(screen.getByText("弹入")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("未选择时跟随风格包默认动效")).toBeInTheDocument();
  });

  it("keeps the other picks when one field is taken back", async () => {
    const user = userEvent.setup();
    const emitted: (SubtitleAnimationPolicy | null)[] = [];
    render(
      <AnimationHarness
        allowClear
        onEmit={(animation) => emitted.push(animation)}
      />,
    );

    await user.click(screen.getByText("滑入"));
    await user.click(screen.getByText("缩小"));
    await user.click(screen.getByText("缩小"));

    expect(emitted.at(-1)).toEqual({ entrance: "slide", exit: null });
  });

  it("offers 恢复默认 once a pick exists and it clears the policy", async () => {
    const user = userEvent.setup();
    const emitted: (SubtitleAnimationPolicy | null)[] = [];
    render(
      <AnimationHarness
        allowClear
        initial={{ entrance: "pop", loop: true }}
        onEmit={(animation) => emitted.push(animation)}
      />,
    );

    const reset = screen.getByRole("button", { name: "恢复默认" });
    await user.click(reset);

    expect(emitted.at(-1)).toBeNull();
    expect(
      screen.queryByRole("button", { name: "恢复默认" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("弹入")).toHaveAttribute("aria-pressed", "false");
  });

  it("keeps picks sticky where clearing is not offered", async () => {
    const user = userEvent.setup();
    render(<AnimationHarness />);

    await user.click(screen.getByText("弹入"));
    await user.click(screen.getByText("弹入"));

    expect(screen.getByText("弹入")).toHaveAttribute("aria-pressed", "true");
    expect(
      screen.queryByRole("button", { name: "恢复默认" }),
    ).not.toBeInTheDocument();
  });
});

function StyleHarness({
  onEmit,
}: {
  onEmit?: (style: SubtitleStyle | null) => void;
}) {
  const [value, setValue] = useState<SubtitleStyle | null>(null);
  return (
    <SubtitleStyleControls
      value={value}
      onChange={(next) => {
        setValue(next);
        onEmit?.(next);
      }}
    />
  );
}

describe("SubtitleStyleControls", () => {
  it("starts on every renderer default", () => {
    render(<StyleHarness />);
    expect(screen.getByText("默认")).toHaveAttribute("aria-pressed", "true");
    // 字号与描边各自的标准档
    const standards = screen.getAllByText("标准");
    expect(standards).toHaveLength(2);
    standards.forEach((node) =>
      expect(node).toHaveAttribute("aria-pressed", "true"),
    );
  });

  it("emits the chosen colour, size and outline", async () => {
    const user = userEvent.setup();
    const emitted: Array<SubtitleStyle | null> = [];
    render(<StyleHarness onEmit={(style) => emitted.push(style)} />);

    await user.click(screen.getByLabelText("#FFE066"));
    await user.click(screen.getByText("大"));
    await user.click(screen.getByText("无"));

    expect(emitted.at(-1)).toEqual({
      color: "#FFE066",
      fontFamily: null,
      fontScale: 1.25,
      strokeScale: 0,
      backgroundOpacity: null,
    });
  });

  it("keeps the plate opacity beside the other overrides", async () => {
    const user = userEvent.setup();
    const emitted: Array<SubtitleStyle | null> = [];
    render(<StyleHarness onEmit={(style) => emitted.push(style)} />);

    // The row starts on the pack's own alpha, which stores nothing.
    expect(screen.getByText("跟随风格")).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    await user.click(screen.getByText("无底板"));
    expect(emitted.at(-1)).toEqual({
      color: null,
      fontFamily: null,
      fontScale: null,
      strokeScale: null,
      backgroundOpacity: 0,
    });

    await user.click(screen.getByText("50%"));
    expect(emitted.at(-1)).toMatchObject({ backgroundOpacity: 0.5 });

    // Going back to 跟随风格 clears the only override, so the whole style
    // object collapses and an untouched project still persists no style.
    await user.click(screen.getByText("跟随风格"));
    expect(emitted.at(-1)).toBeNull();
  });

  it("collapses back to null once every override is cleared", async () => {
    const user = userEvent.setup();
    const emitted: Array<SubtitleStyle | null> = [];
    render(<StyleHarness onEmit={(style) => emitted.push(style)} />);

    await user.click(screen.getByLabelText("#7DD3FC"));
    expect(emitted.at(-1)).not.toBeNull();

    await user.click(screen.getByText("默认"));
    expect(emitted.at(-1)).toBeNull();
  });

  it("offers 恢复默认 only while an override is stated, and it clears all", async () => {
    const user = userEvent.setup();
    const emitted: Array<SubtitleStyle | null> = [];
    render(<StyleHarness onEmit={(style) => emitted.push(style)} />);

    // An untouched style has nothing to reset, so the button stays away — the
    // same rule the motion picker's 恢复默认 follows.
    expect(screen.queryByRole("button", { name: "恢复默认" })).toBeNull();

    await user.click(screen.getByLabelText("#FFE066"));
    await user.click(screen.getByRole("button", { name: "恢复默认" }));
    expect(emitted.at(-1)).toBeNull();
    expect(screen.queryByRole("button", { name: "恢复默认" })).toBeNull();
  });

  it("paints each font pill in its own face, the auto one inheriting", () => {
    render(<StyleHarness />);
    expect(
      screen.getByRole("button", { name: "宋体" }).style.fontFamily,
    ).toContain("Songti SC");
    expect(
      screen.getByRole("button", { name: "黑体" }).style.fontFamily,
    ).toContain("PingFang SC");
    expect(
      screen.getByRole("button", { name: "楷体" }).style.fontFamily,
    ).toContain("Kaiti SC");
    expect(
      screen.getByRole("button", { name: "默认字体" }).style.fontFamily,
    ).toBe("");
  });
});
