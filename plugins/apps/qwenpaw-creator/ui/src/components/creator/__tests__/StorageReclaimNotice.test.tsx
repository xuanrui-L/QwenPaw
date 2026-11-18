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
const awaitingConfirmation = {
  pendingReclaimBytes: 5 * 1024 * 1024 * 1024,
  historyKeptAt: null,
  reclaimConfirmedAt: null,
};

describe("StorageReclaimNotice", () => {
  beforeEach(() => {
    mockedReclaim.mockReset();
    // antd caches its message holder node; emptying <body> would leave the
    // next message appended to a detached element. Imperative confirm modals
    // are portals outside the rendered tree, so cleanup cannot remove them.
    message.destroy();
    Modal.destroyAll();
    // destroyAll() unmounts instances but leaves closing portals in the DOM
    // until their animation ticks; a leftover OK button would swallow the
    // next case's click, so purge the nodes outright.
    document
      .querySelectorAll(".ant-modal-root, .ant-message")
      .forEach((node) => node.remove());
  });

  it("names the waiting bytes and offers the one press", () => {
    render(
      <StorageReclaimNotice
        projectId="project-a"
        projectName="被逐出宗门那天"
        storage={awaitingConfirmation}
        reclaimCap={1}
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
    const { container } = render(
      <StorageReclaimNotice
        projectId="project-a"
        projectName="被逐出宗门那天"
        storage={awaitingConfirmation}
        reclaimCap={1}
        onConfirmed={onConfirmed}
      />,
    );

    // Query inside the rendered notice: a modal portal left closing by an
    // earlier case repeats the same label in document order.
    fireEvent.click(
      container.querySelector("[data-storage-reclaim-notice] button")!,
    );
    // The confirm dialog repeats the label as its OK button; the last match
    // in document order is the dialog's.
    // The newest modal root owns the OK button; a portal left closing by an
    // earlier case still repeats the label in role order.
    const okButton = await waitFor(() => {
      const modals = Array.from(document.querySelectorAll(".ant-modal"));
      expect(modals.length).toBeGreaterThan(0);
      const ok = (modals[modals.length - 1] as HTMLElement).querySelector(
        ".ant-btn-primary",
      ) as HTMLElement | null;
      expect(ok).not.toBeNull();
      return ok as HTMLElement;
    });
    fireEvent.click(okButton);

    // The press is the decision: the notice steps aside before the sweep
    // answers, instead of spinning for the whole deletion.
    expect(document.querySelector("[data-storage-reclaim-notice]")).toBeNull();
    await waitFor(() =>
      expect(mockedReclaim).toHaveBeenCalledWith("project-a"),
    );
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("已释放 3.0 KB 沉淀历史"),
    ).toBeInTheDocument();
  });

  it("retries only the remaining confirmed cleanup and reloads the profile", async () => {
    const receipt = "2026-11-03T00:00:00Z";
    const storage = { ...awaitingConfirmation, reclaimConfirmedAt: receipt };
    const onConfirmed = vi.fn();
    const props = {
      projectId: "project-a",
      projectName: "Archive",
      reclaimCap: 1024,
      onConfirmed,
    };
    const { rerender } = render(
      <StorageReclaimNotice {...props} storage={storage} />,
    );
    expect(screen.queryByRole("button")).toBeNull(); // Older response omitted cleanup bytes.
    rerender(
      <StorageReclaimNotice
        {...props}
        storage={{
          ...storage,
          pendingTransactionCleanupBytes: 64,
          historyKeptAt: receipt,
        }}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
    rerender(
      <StorageReclaimNotice
        {...props}
        storage={{ ...storage, pendingTransactionCleanupBytes: 64 }}
      />,
    );
    mockedReclaim.mockResolvedValue({
      projectId: "project-a",
      historyKept: false,
      confirmedAt: receipt,
      removedTransactions: 1,
      transactionBytes: 64,
      releasedFiles: 0,
      releasedBytes: 0,
    });
    fireEvent.click(screen.getByRole("button", { name: "继续清理" }));
    await waitFor(() => expect(onConfirmed).toHaveBeenCalledOnce());
    expect(mockedReclaim).toHaveBeenCalledWith("project-a");
    rerender(
      <StorageReclaimNotice
        {...props}
        storage={{ ...storage, pendingTransactionCleanupBytes: 0 }}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("brings the notice back when the press fails", async () => {
    mockedReclaim.mockRejectedValue(new Error("boom"));
    // The static antd holder races Modal.destroyAll() plus the now-immediate
    // modal close, so pin the call rather than the rendered bubble.
    const errorSpy = vi.spyOn(message, "error");
    const onConfirmed = vi.fn();
    const { container } = render(
      <StorageReclaimNotice
        projectId="project-a"
        projectName="被逐出宗门那天"
        storage={awaitingConfirmation}
        reclaimCap={1}
        onConfirmed={onConfirmed}
      />,
    );

    // Query inside the rendered notice: a modal portal left closing by an
    // earlier case repeats the same label in document order.
    fireEvent.click(
      container.querySelector("[data-storage-reclaim-notice] button")!,
    );
    // The confirm dialog repeats the label as its OK button; the last match
    // in document order is the dialog's.
    // The newest modal root owns the OK button; a portal left closing by an
    // earlier case still repeats the label in role order.
    const okButton = await waitFor(() => {
      const modals = Array.from(document.querySelectorAll(".ant-modal"));
      expect(modals.length).toBeGreaterThan(0);
      const ok = (modals[modals.length - 1] as HTMLElement).querySelector(
        ".ant-btn-primary",
      ) as HTMLElement | null;
      expect(ok).not.toBeNull();
      return ok as HTMLElement;
    });
    fireEvent.click(okButton);

    // The rejection lands in the same flush, so what this case pins is the
    // recovery: the notice comes back and the refusal is reported.
    await waitFor(() => expect(mockedReclaim).toHaveBeenCalled());
    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
    expect(String(errorSpy.mock.calls[0][0])).toMatch(/无法释放沉淀历史/);
    await waitFor(() =>
      expect(
        document.querySelector("[data-storage-reclaim-notice]"),
      ).not.toBeNull(),
    );
    expect(onConfirmed).toHaveBeenCalledTimes(1);
  });
});
