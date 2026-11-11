import { useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { SubtitleStyle } from "@/contracts/creator";
import SubtitleTemplatePicker, {
  SUBTITLE_TEMPLATE_PACKS,
} from "../SubtitleTemplatePicker";

function Harness({ style = null }: { style?: SubtitleStyle | null }) {
  const [value, setValue] = useState<string | null>(null);
  return (
    <SubtitleTemplatePicker value={value} onChange={setValue} style={style} />
  );
}

function previewWords(container: HTMLElement, id: string): HTMLElement {
  return container.querySelector(
    `[data-subtitle-preview="${id}"]`,
  ) as HTMLElement;
}

describe("SubtitleTemplatePicker", () => {
  it("offers smart recommend plus every style pack", () => {
    render(<Harness />);
    // 1 smart + 5 packs
    expect(screen.getAllByRole("button")).toHaveLength(
      SUBTITLE_TEMPLATE_PACKS.length + 1,
    );
    expect(screen.getByText("智能推荐")).toBeInTheDocument();
    expect(screen.getByText("综艺花字")).toBeInTheDocument();
    expect(screen.getByText("霓虹音乐")).toBeInTheDocument();
  });

  it("selects a pack then returns to smart recommend", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const variety = screen.getByText("综艺花字").closest("button");
    await user.click(variety!);
    expect(variety).toHaveAttribute("aria-pressed", "true");

    const smart = screen.getByText("智能推荐").closest("button");
    await user.click(smart!);
    expect(smart).toHaveAttribute("aria-pressed", "true");
    expect(variety).toHaveAttribute("aria-pressed", "false");
  });

  it("paints renderer-homogeneous preview styles per pack", () => {
    const { container } = render(<Harness />);
    const cinematic = previewWords(container, "cinematic");
    expect(cinematic.style.fontFamily).toContain("Songti");
    expect(cinematic.style.letterSpacing).toBe("0.18em");
    // clean_talk draws its pack plate at the pack's own alpha.
    const clean = previewWords(container, "clean_talk");
    expect(clean.style.backgroundColor).toContain("0.42");
    // variety_pop strokeScale 1.35 → first shadow layer 0.025*1.35.
    const variety = previewWords(container, "variety_pop");
    expect(variety.style.textShadow).toContain(".0338em");
    // neon_music stacks the glow halo on top of the outline.
    const neon = previewWords(container, "neon_music");
    expect(neon.style.textShadow).toContain(".06em");
  });

  it("draft style reshapes plate alpha and leaves plate-free packs alone", () => {
    const { container, rerender } = render(
      <Harness style={{ backgroundOpacity: 0.7 }} />,
    );
    expect(
      previewWords(container, "clean_talk").style.backgroundColor,
    ).toContain("0.7");
    expect(previewWords(container, "variety_pop").style.backgroundColor).toBe(
      "",
    );
    rerender(<Harness style={{ backgroundOpacity: 0 }} />);
    expect(previewWords(container, "clean_talk").style.backgroundColor).toBe(
      "",
    );
  });

  it("falls back to the mirror when the preview endpoint is unreachable", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const { container } = render(<Harness />);
      await waitFor(() => expect(fetchMock).toHaveBeenCalled());
      expect(previewWords(container, "cinematic").style.letterSpacing).toBe(
        "0.18em",
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("prefers backend specs over the mirror", async () => {
    const items = [
      {
        id: "variety_pop",
        fontFamily: "'Test Sans',sans-serif",
        fontWeight: 800,
        letterSpacing: "0.99em",
        lineHeight: 1.25,
        strokeScale: 1.35,
        tiltDeg: 0,
        plate: "",
        plateRadius: "0.28em",
        glow: false,
        entrance: "pop",
        loop: true,
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        headers: new Headers(),
        json: async () => ({ items }),
      }),
    );
    try {
      const { container } = render(<Harness />);
      await waitFor(() =>
        expect(
          previewWords(container, "variety_pop").style.letterSpacing,
        ).toBe("0.99em"),
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
