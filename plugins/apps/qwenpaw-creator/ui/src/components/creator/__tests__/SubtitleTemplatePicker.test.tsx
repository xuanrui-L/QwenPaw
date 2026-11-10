import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import SubtitleTemplatePicker, {
  SUBTITLE_TEMPLATE_PACKS,
} from "../SubtitleTemplatePicker";

function Harness() {
  const [value, setValue] = useState<string | null>(null);
  return (
    <SubtitleTemplatePicker value={value} onChange={setValue} />
  );
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
});
