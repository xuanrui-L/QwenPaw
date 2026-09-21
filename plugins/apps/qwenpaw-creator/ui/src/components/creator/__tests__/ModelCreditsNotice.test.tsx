import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import ModelCreditsNotice from "../ModelCreditsNotice";
import { useModelCreditsStore } from "@/store/modelCreditsStore";

describe("ModelCreditsNotice", () => {
  beforeEach(() => useModelCreditsStore.setState({ notice: null }));

  it("says nothing while the balance is unknown or healthy", () => {
    const { container } = render(<ModelCreditsNotice />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the reason without offering a jump to the failing project", () => {
    // Reaching that project cannot fix an empty balance, and the pill used to
    // navigate there on click. It is also a plain element rather than a
    // component for a reason: antd measures the tooltip anchor through a ref,
    // and a child that does not forward one leaves the bubble at the top-left
    // of the document, appearing only on mouse-leave.
    useModelCreditsStore.getState().markExhausted("project-ac976efc");
    render(<ModelCreditsNotice />);

    const pill = screen.getByText("Credits 不足");
    expect(pill).toBeInTheDocument();
    expect(pill.closest("a")).toBeNull();
    expect(document.querySelector("[data-model-credits-pill]")).not.toBeNull();
  });

  it("hides on request until the next refusal proves it again", () => {
    useModelCreditsStore.getState().markExhausted("p1");
    render(<ModelCreditsNotice />);

    fireEvent.click(
      screen.getByRole("button", { name: "暂时隐藏 Credits 提醒" }),
    );

    expect(screen.queryByText("Credits 不足")).not.toBeInTheDocument();
    expect(useModelCreditsStore.getState().notice).toBeNull();
  });
});
