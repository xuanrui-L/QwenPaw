import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { SubtitleStyle } from "@/contracts/creator";
import SubtitleStyleControls from "../SubtitleStyleControls";

function Harness({
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
    render(<Harness />);
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
    render(<Harness onEmit={(style) => emitted.push(style)} />);

    await user.click(screen.getByLabelText("#FFE066"));
    await user.click(screen.getByText("大"));
    await user.click(screen.getByText("无"));

    expect(emitted.at(-1)).toEqual({
      color: "#FFE066",
      fontScale: 1.25,
      strokeScale: 0,
    });
  });

  it("collapses back to null once every override is cleared", async () => {
    const user = userEvent.setup();
    const emitted: Array<SubtitleStyle | null> = [];
    render(<Harness onEmit={(style) => emitted.push(style)} />);

    await user.click(screen.getByLabelText("#7DD3FC"));
    expect(emitted.at(-1)).not.toBeNull();

    await user.click(screen.getByText("默认"));
    expect(emitted.at(-1)).toBeNull();
  });
});
