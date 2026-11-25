import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  deleteUserSubtitleStyle,
  listUserSubtitleStyles,
  saveUserSubtitleStyle,
  updateUserSubtitleStyle,
} from "@/api/creator/subtitleStyles";
import type { SubtitleStyle } from "@/contracts/creator";
import SubtitleTemplatePicker, {
  SUBTITLE_TEMPLATE_PACKS,
} from "../SubtitleTemplatePicker";
import SubtitleSettingsPicker from "../SubtitleSettingsPicker";

vi.mock("@/api/creator/subtitleStyles", () => ({
  listUserSubtitleStyles: vi.fn(),
  saveUserSubtitleStyle: vi.fn(),
  updateUserSubtitleStyle: vi.fn(),
  deleteUserSubtitleStyle: vi.fn(),
}));

const CUSTOM = {
  styleId: "user:abc",
  name: "我的综艺字",
  description: "高饱和",
  packId: "variety_pop",
  style: { color: "#ff0000" },
  animation: { entrance: "pop" as const },
};

beforeEach(() => {
  vi.mocked(listUserSubtitleStyles).mockResolvedValue({ items: [] });
  vi.mocked(saveUserSubtitleStyle).mockResolvedValue(CUSTOM);
  vi.mocked(updateUserSubtitleStyle).mockResolvedValue(CUSTOM);
  vi.mocked(deleteUserSubtitleStyle).mockResolvedValue({
    deleted: CUSTOM.styleId,
  });
});

function TemplateHarness({ style = null }: { style?: SubtitleStyle | null }) {
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
    render(<TemplateHarness />);
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
    render(<TemplateHarness />);
    const variety = screen.getByText("综艺花字").closest("button");
    await user.click(variety!);
    expect(variety).toHaveAttribute("aria-pressed", "true");

    const smart = screen.getByText("智能推荐").closest("button");
    await user.click(smart!);
    expect(smart).toHaveAttribute("aria-pressed", "true");
    expect(variety).toHaveAttribute("aria-pressed", "false");
  });

  it("paints renderer-homogeneous preview styles per pack", () => {
    const { container } = render(<TemplateHarness />);
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
      <TemplateHarness style={{ backgroundOpacity: 0.7 }} />,
    );
    expect(
      previewWords(container, "clean_talk").style.backgroundColor,
    ).toContain("0.7");
    expect(previewWords(container, "variety_pop").style.backgroundColor).toBe(
      "",
    );
    rerender(<TemplateHarness style={{ backgroundOpacity: 0 }} />);
    expect(previewWords(container, "clean_talk").style.backgroundColor).toBe(
      "",
    );
  });

  it("drafts the picked face onto every card so the font previews live", () => {
    const { container, rerender } = render(
      <TemplateHarness style={{ fontFamily: "kai" }} />,
    );
    // Every miniature speaks the draft face, not just the pack that ships it.
    expect(previewWords(container, "variety_pop").style.fontFamily).toContain(
      "Kaiti",
    );
    expect(previewWords(container, "cinematic").style.fontFamily).toContain(
      "Kaiti",
    );
    // The footnote names the face it now paints.
    expect(screen.getAllByText(/楷体/).length).toBeGreaterThan(0);

    // Auto hands the cards back to each pack's own typography.
    rerender(<TemplateHarness style={{ fontFamily: "default" }} />);
    expect(previewWords(container, "cinematic").style.fontFamily).toContain(
      "Songti",
    );
    expect(previewWords(container, "variety_pop").style.fontFamily).toContain(
      "PingFang",
    );
  });

  it("paints a saved bundle's own face on its card", async () => {
    vi.mocked(listUserSubtitleStyles).mockResolvedValue({
      items: [{ ...CUSTOM, style: { fontFamily: "song" as const } }],
    });
    const { container } = render(
      <SubtitleTemplatePicker
        value="variety_pop"
        onChange={vi.fn()}
        onSelectCustom={vi.fn()}
      />,
    );
    await screen.findByText("我的综艺字");
    // The bundle saved a serif pick; its own card must not fall back to the
    // base pack's sans face.
    expect(previewWords(container, "user:abc").style.fontFamily).toContain(
      "Songti",
    );
  });

  it("falls back to the mirror when the preview endpoint is unreachable", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const { container } = render(<TemplateHarness />);
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
      const { container } = render(<TemplateHarness />);
      await waitFor(() =>
        expect(previewWords(container, "variety_pop").style.letterSpacing).toBe(
          "0.99em",
        ),
      );
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("shows saved bundles and applies them in one move", async () => {
    const user = userEvent.setup();
    vi.mocked(listUserSubtitleStyles).mockResolvedValue({ items: [CUSTOM] });
    const onSelectCustom = vi.fn();
    render(
      <SubtitleTemplatePicker
        value={null}
        onChange={vi.fn()}
        onSelectCustom={onSelectCustom}
      />,
    );

    const card = await screen.findByText("我的综艺字");
    expect(screen.getByText("自定义")).toBeInTheDocument();
    await user.click(card.closest("button")!);

    expect(onSelectCustom).toHaveBeenCalledWith(CUSTOM);
  });

  it("lights the saved bundle whose picks match the window", async () => {
    vi.mocked(listUserSubtitleStyles).mockResolvedValue({ items: [CUSTOM] });
    const card = await (async () => {
      const view = render(
        <SubtitleTemplatePicker
          value="variety_pop"
          onChange={vi.fn()}
          style={{ color: "#ff0000" }}
          animation={{ entrance: "pop" }}
          onSelectCustom={vi.fn()}
        />,
      );
      return view.findByText("我的综艺字");
    })();
    await waitFor(() =>
      expect(card.closest("button")).toHaveAttribute("aria-pressed", "true"),
    );
    // The base pack card of the same pack stays unlit.
    expect(screen.getByText("综艺花字").closest("button")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("does not light a bundle whose only difference is the face pick", async () => {
    vi.mocked(listUserSubtitleStyles).mockResolvedValue({
      items: [
        {
          ...CUSTOM,
          style: { color: "#ff0000", fontFamily: "song" as const },
        },
      ],
    });
    render(
      <SubtitleTemplatePicker
        value="variety_pop"
        onChange={vi.fn()}
        style={{ color: "#ff0000", fontFamily: "kai" }}
        animation={{ entrance: "pop" }}
        onSelectCustom={vi.fn()}
      />,
    );
    const card = await screen.findByText("我的综艺字");
    expect(card.closest("button")).toHaveAttribute("aria-pressed", "false");
  });

  it("treats auto and an absent face as the same selection", async () => {
    vi.mocked(listUserSubtitleStyles).mockResolvedValue({
      items: [{ ...CUSTOM, style: { color: "#ff0000", fontFamily: null } }],
    });
    render(
      <SubtitleTemplatePicker
        value="variety_pop"
        onChange={vi.fn()}
        style={{ color: "#ff0000", fontFamily: "default" }}
        animation={{ entrance: "pop" }}
        onSelectCustom={vi.fn()}
      />,
    );
    const card = await screen.findByText("我的综艺字");
    const lit = await waitFor(() => {
      const button = card.closest("button");
      expect(button).toHaveAttribute("aria-pressed", "true");
      return button;
    });
    expect(lit).toHaveAttribute("aria-pressed", "true");
  });

  it("saves the current picks under a name", async () => {
    const user = userEvent.setup();
    render(
      <SubtitleTemplatePicker
        value="variety_pop"
        onChange={vi.fn()}
        style={{ color: "#ff0000" }}
        animation={{ entrance: "pop" }}
        onSelectCustom={vi.fn()}
      />,
    );

    await user.click(screen.getByText(/保存当前风格/));
    await user.type(screen.getByPlaceholderText("风格名称"), "我的综艺字");
    await user.click(screen.getByRole("button", { name: /确\s*定/ }));

    expect(saveUserSubtitleStyle).toHaveBeenCalledWith({
      name: "我的综艺字",
      description: "",
      packId: "variety_pop",
      style: { color: "#ff0000" },
      animation: { entrance: "pop" },
    });
  });

  it("updates and deletes a saved bundle from its card actions", async () => {
    vi.mocked(listUserSubtitleStyles).mockResolvedValue({ items: [CUSTOM] });
    render(
      <SubtitleTemplatePicker
        value={null}
        onChange={vi.fn()}
        onSelectCustom={vi.fn()}
      />,
    );
    await screen.findByText("我的综艺字");

    // The actions ride on each custom card's top-right corner.
    fireEvent.click(screen.getByRole("button", { name: "更新" }));
    expect(updateUserSubtitleStyle).toHaveBeenCalledWith("user:abc", {
      name: "我的综艺字",
      description: "高饱和",
      packId: null,
      style: null,
      animation: null,
    });

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    // antd spaces the two CJK chars of the popconfirm's ok button.
    fireEvent.click(screen.getByRole("button", { name: "删 除" }));
    expect(deleteUserSubtitleStyle).toHaveBeenCalledWith("user:abc");
  });
});

function renderPicker(
  overrides: Partial<React.ComponentProps<typeof SubtitleSettingsPicker>> = {},
) {
  const view: React.ComponentProps<typeof SubtitleSettingsPicker> = {
    policy: "off",
    onPolicyChange: vi.fn(),
    template: null,
    onTemplateChange: vi.fn(),
    style: null,
    onStyleChange: vi.fn(),
    ...overrides,
  };
  render(<SubtitleSettingsPicker {...view} />);
  return view;
}

/** A stateful host, so the flows whose window state follows the policy work. */
function SettingsHarness() {
  const [policy, setPolicy] = useState("off");
  const [template, setTemplate] = useState<string | null>(null);
  const [style, setStyle] = useState<SubtitleStyle | null>(null);
  return (
    <SubtitleSettingsPicker
      policy={policy}
      onPolicyChange={setPolicy}
      template={template}
      onTemplateChange={setTemplate}
      style={style}
      onStyleChange={setStyle}
    />
  );
}

function openDropdown() {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "字幕" }));
}

async function pickOption(name: string) {
  openDropdown();
  await waitFor(() => expect(screen.getByText(name)).toBeTruthy());
  fireEvent.click(screen.getByText(name).closest(".ant-select-item-option")!);
}

/**
 * A Modal that has been shown keeps its content mounted while it is hidden, so
 * the question is whether the window is on screen, not whether its DOM is.
 */
async function expectWindow(open: boolean) {
  await waitFor(() => {
    const panel = document.querySelector("[data-subtitle-style-modal]");
    if (open) {
      expect(panel).toBeInTheDocument();
      expect(panel).toBeVisible();
    } else if (panel) {
      expect(panel).not.toBeVisible();
    }
  });
}

describe("SubtitleSettingsPicker", () => {
  it("opens the style window on 自动 and drops it on 关闭", async () => {
    render(<SettingsHarness />);
    await pickOption("自动");
    await expectWindow(true);
    const panel = document.querySelector("[data-subtitle-style-modal]")!;
    expect(panel.textContent).toContain("智能推荐");
    expect(panel.textContent).toContain("综艺花字");
    expect(panel.textContent).toContain("字幕样式");

    await pickOption("关闭");
    await expectWindow(false);
  });

  it("keeps the window reachable while the policy stays auto", async () => {
    render(<SettingsHarness />);
    await pickOption("自动");
    await expectWindow(true);

    // antd spaces two CJK characters in a button, hence the optional gap.
    fireEvent.click(screen.getByRole("button", { name: /完\s*成/ }));
    await expectWindow(false);

    // The value does not change the second time, but picking 自动 again is the
    // only way back into the window once it has been closed.
    await pickOption("自动");
    await expectWindow(true);
  });

  it("sizes the window to the viewport instead of a fixed panel", async () => {
    render(<SettingsHarness />);
    await pickOption("自动");
    await expectWindow(true);

    const modal = document.querySelector(".ant-modal") as HTMLElement;
    expect(modal.style.maxWidth).toBe("calc(100vw - 32px)");
    const body = document.querySelector(".ant-modal-body") as HTMLElement;
    expect(body.style.overflowY).toBe("auto");
    expect(body.style.maxHeight).toBeTruthy();
  });

  it("carries a pack pick out of the window", async () => {
    const view = renderPicker({ policy: "auto" });
    await pickOption("自动");
    await expectWindow(true);
    fireEvent.click(screen.getByText("综艺花字"));
    expect(view.onTemplateChange).toHaveBeenCalledWith("variety_pop");
  });

  it("carries a motion pick out of the window", async () => {
    const view = renderPicker({
      policy: "auto",
      onAnimationChange: vi.fn(),
    });
    await pickOption("自动");
    await expectWindow(true);
    const panel = document.querySelector("[data-subtitle-style-modal]")!;
    expect(panel.textContent).toContain("字幕动效");
    fireEvent.click(screen.getByRole("button", { name: "滑入" }));
    expect(view.onAnimationChange).toHaveBeenCalledWith({
      entrance: "slide",
    });
  });

  it("takes a motion pick back to the pack default", async () => {
    const view = renderPicker({
      policy: "auto",
      animation: { entrance: "slide" },
      onAnimationChange: vi.fn(),
    });
    await pickOption("自动");
    await expectWindow(true);

    // The lit pill toggles back off, and an emptied policy reads as null.
    fireEvent.click(screen.getByRole("button", { name: "滑入" }));
    expect(view.onAnimationChange).toHaveBeenLastCalledWith(null);

    fireEvent.click(screen.getByRole("button", { name: "恢复默认" }));
    expect(view.onAnimationChange).toHaveBeenLastCalledWith(null);
  });

  it("hides the motion picker when the host cannot persist it", async () => {
    renderPicker({ policy: "auto" });
    await pickOption("自动");
    await expectWindow(true);
    const panel = document.querySelector("[data-subtitle-style-modal]")!;
    expect(panel.textContent).not.toContain("字幕动效");
  });
});
