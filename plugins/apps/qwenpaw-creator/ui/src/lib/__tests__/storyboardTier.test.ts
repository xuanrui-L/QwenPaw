import { describe, expect, it } from "vitest";

import { storyboardTierForElement } from "../storyboardTier";

const creation = (over: Record<string, unknown> = {}) => ({
  type: "r2v",
  scene_ref: "scene:stop",
  character_refs: ["char:boy"],
  prop_refs: [] as string[],
  continuity: "承接上一段…",
  video_prompt: "男孩站在顶棚下低头看手机。",
  narrative: "男孩看手机。",
  force_storyboard: false,
  ...over,
});

const el = (
  id: string,
  start: number,
  duration: number,
  over: Record<string, unknown> = {},
) => ({
  element_id: id,
  enabled: true,
  span: { start_tick: start, duration_tick: duration },
  creation: creation(over),
});

const project = (elements: unknown[], settings: Record<string, unknown> = {}) =>
  ({
    settings: { video_continuity: null, ...settings },
    timelines: {
      items: {
        "timeline:main": {
          ticks_per_second: 10,
          elements_by_id: Object.fromEntries(
            (elements as any[]).map((item) => [item.element_id, item]),
          ),
        },
      },
    },
  }) as any;

describe("storyboardTierForElement", () => {
  it("first segment is plain", () => {
    const p = project([el("e1", 0, 50)]);
    expect(storyboardTierForElement(p, "e1")).toBe("plain");
  });

  it("simple anchored segment with authored intent is none", () => {
    const p = project([
      el("e1", 0, 50),
      el("e2", 50, 50, {
        transition_intent: "continue",
        storyboard_intent: "none",
      }),
    ]);
    expect(storyboardTierForElement(p, "e2")).toBe("none");
  });

  it("missing intent fields take conservative defaults", () => {
    const plain = project([el("e1", 0, 50), el("e2", 50, 50)]);
    expect(storyboardTierForElement(plain, "e2")).toBe("plain");
    const rides = project([
      el("e1", 0, 50),
      el("e2", 50, 50, { transition_intent: "continue" }),
    ]);
    expect(storyboardTierForElement(rides, "e2")).toBe("combined");
  });

  it("long anchored segment is combined", () => {
    const p = project([
      el("e1", 0, 50),
      el("e2", 50, 120, { transition_intent: "continue" }),
    ]);
    expect(storyboardTierForElement(p, "e2")).toBe("combined");
  });

  it("hard constraints and authored need are combined", () => {
    expect(
      storyboardTierForElement(
        project([
          el("e1", 0, 50, { character_refs: ["char:a", "char:b"] }),
          el("e2", 50, 50, {
            character_refs: ["char:a", "char:b"],
            transition_intent: "continue",
          }),
        ]),
        "e2",
      ),
    ).toBe("combined");
    expect(
      storyboardTierForElement(
        project([
          el("e1", 0, 50),
          el("e2", 50, 50, {
            video_prompt: "镜头缓推向站台。",
            transition_intent: "continue",
            storyboard_intent: "needed",
          }),
        ]),
        "e2",
      ),
    ).toBe("combined");
  });

  it("prose alone never routes; authored none keeps it simple", () => {
    expect(
      storyboardTierForElement(
        project([
          el("e1", 0, 50),
          el("e2", 50, 50, {
            video_prompt: "伞撑开在两人头顶。",
            transition_intent: "continue",
            storyboard_intent: "none",
          }),
        ]),
        "e2",
      ),
    ).toBe("none");
    expect(
      storyboardTierForElement(
        project([
          el("e1", 0, 50, {
            character_refs: ["char:boy", "char:girl"],
            prop_refs: ["prop:umbrella"],
          }),
          el("e2", 50, 50, {
            video_prompt: "伞撑开在头顶。",
            prop_refs: ["prop:umbrella"],
            character_refs: ["char:boy", "char:girl"],
            transition_intent: "continue",
          }),
        ]),
        "e2",
      ),
    ).toBe("combined");
  });

  it("scene change and authored cut fall back to plain", () => {
    expect(
      storyboardTierForElement(
        project([
          el("e1", 0, 50),
          el("e2", 50, 50, { scene_ref: "scene:inside" }),
        ]),
        "e2",
      ),
    ).toBe("plain");
    expect(
      storyboardTierForElement(
        project([
          el("e1", 0, 50),
          el("e2", 50, 50, { continuity: "切到另一机位" }),
        ]),
        "e2",
      ),
    ).toBe("plain");
    expect(
      storyboardTierForElement(
        project([
          el("e1", 0, 50),
          el("e2", 50, 50, { transition_intent: "cut" }),
        ]),
        "e2",
      ),
    ).toBe("plain");
  });

  it("force_storyboard pins combined", () => {
    const p = project([el("e1", 0, 50), el("e2", 50, 50)]);
    (
      p.timelines.items["timeline:main"] as any
    ).elements_by_id.e2.creation.force_storyboard = true;
    expect(storyboardTierForElement(p, "e2")).toBe("combined");
  });

  it("explicit transition between neighbours demotes to plain", () => {
    const transition = {
      element_id: "t1",
      enabled: true,
      span: { start_tick: 50, duration_tick: 1 },
      creation: {
        type: "transition",
        from_element_id: "e1",
        to_element_id: "e2",
        transition_kind: "fade",
      },
    };
    const p = project([el("e1", 0, 50), el("e2", 50, 50), transition]);
    expect(storyboardTierForElement(p, "e2")).toBe("plain");
  });

  it("structured intent outranks the keyword mirror", () => {
    const negated = project([
      el("e1", 0, 50),
      el("e2", 50, 50, {
        continuity: "不要切到另一机位",
        transition_intent: "continue",
        storyboard_intent: "none",
      }),
    ]);
    expect(storyboardTierForElement(negated, "e2")).toBe("none");

    const cut = project([
      el("e1", 0, 50),
      el("e2", 50, 50, {
        continuity: "延续运镜推进",
        transition_intent: "cut",
      }),
    ]);
    expect(storyboardTierForElement(cut, "e2")).toBe("plain");

    const needed = project([
      el("e1", 0, 50),
      el("e2", 50, 50, {
        transition_intent: "continue",
        storyboard_intent: "needed",
      }),
    ]);
    expect(storyboardTierForElement(needed, "e2")).toBe("combined");

    // The model decides: authored "none" wins even for a long take;
    // hard duration rules no longer override creative intent.
    const longNone = project([
      el("e1", 0, 50),
      el("e2", 50, 120, {
        transition_intent: "continue",
        storyboard_intent: "none",
      }),
    ]);
    expect(storyboardTierForElement(longNone, "e2")).toBe("none");
  });
});
