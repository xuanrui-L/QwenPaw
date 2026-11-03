import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { message, Modal } from "antd";
import { beforeEach, describe, expect, it, vi } from "vitest";
import StorageReclaimNotice from "../StorageReclaimNotice";
import { confirmProjectReclaim } from "@/api/creator";

vi.mock("@/api/creator", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/creator")>();
  return { ...actual, confirmProjectReclaim: vi.fn() };
});

const mockedReclaim = vi.mocked(confirmProjectReclaim);

describe("StorageReclaimNotice", () => {
  beforeEach(() => {
    mockedReclaim.mockReset();
    // antd caches its message holder node; emptying <body> would leave the
    // next message appended to a detached element. Imperative confirm modals
    // are portals outside the rendered tree, so cleanup cannot remove them.
    message.destroy();
    Modal.destroyAll();
  });

  it("names the waiting bytes and offers the one press", () => {
    render(
      <StorageReclaimNotice
        projectId="project-a"
        projectName="被逐出宗门那天"
        pendingBytes={5 * 1024 * 1024 * 1024}
        onConfirmed={() => {}}
      />,
    );

    expect(
      document.querySelector("[data-storage-reclaim-notice]"),
    ).not.toBeNull();
    expect(
      screen.getByText("已有 5.0 GB 沉淀历史等待你确认"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "确认并释放" }),
    ).toBeInTheDocument();
  });

  it("records the confirmation and reports what the sweep freed", async () => {
    mockedReclaim.mockResolvedValue({
      projectId: "project-a",
      historyKept: false,
      removedTransactions: 12,
      transactionBytes: 1024,
      releasedFiles: 3,
      releasedBytes: 2048,
      confirmedAt: "2026-11-03T00:00:00+00:00",
    });
    const onConfirmed = vi.fn();
    render(
      <StorageReclaimNotice
        projectId="project-a"
        projectName="被逐出宗门那天"
        pendingBytes={5 * 1024 * 1024 * 1024}
        onConfirmed={onConfirmed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "确认并释放" }));
    // The confirm dialog repeats the label as its OK button; the last match
    // in document order is the dialog's, the first is the notice's own.
    const buttons = await waitFor(() => {
      const found = screen.getAllByRole("button", { name: "确认并释放" });
      expect(found.length).toBeGreaterThan(1);
      return found;
    });
    fireEvent.click(buttons[buttons.length - 1]);

    await waitFor(() =>
      expect(mockedReclaim).toHaveBeenCalledWith("project-a"),
    );
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("已释放 3.0 KB 沉淀历史"),
    ).toBeInTheDocument();
  });

  it("keeps the notice when the press fails", async () => {
    mockedReclaim.mockRejectedValue(new Error("boom"));
    const onConfirmed = vi.fn();
    render(
      <StorageReclaimNotice
        projectId="project-a"
        projectName="被逐出宗门那天"
        pendingBytes={5 * 1024 * 1024 * 1024}
        onConfirmed={onConfirmed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "确认并释放" }));
    // The confirm dialog repeats the label as its OK button; the last match
    // in document order is the dialog's, the first is the notice's own.
    const buttons = await waitFor(() => {
      const found = screen.getAllByRole("button", { name: "确认并释放" });
      expect(found.length).toBeGreaterThan(1);
      return found;
    });
    fireEvent.click(buttons[buttons.length - 1]);

    expect(await screen.findByText(/无法释放沉淀历史/)).toBeInTheDocument();
    expect(onConfirmed).not.toHaveBeenCalled();
  });
});
