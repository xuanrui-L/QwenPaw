import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { message } from "antd";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProjectImporter } from "../ProjectImportExport";

class ImportRequest {
  static current: ImportRequest;
  upload = {
    onprogress: null as ((event: ProgressEvent) => void) | null,
    onload: null as (() => void) | null,
  };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  status = 0;
  responseText = "";
  open = vi.fn();
  setRequestHeader = vi.fn();
  send = vi.fn();
  constructor() {
    ImportRequest.current = this;
  }
}

afterEach(() => vi.unstubAllGlobals());

describe("project archive import", () => {
  it.each([
    { outcome: "success", lengthComputable: true },
    { outcome: "failure", lengthComputable: false },
  ])(
    "waits for the server after upload completes, then handles $outcome",
    async ({ outcome, lengthComputable }) => {
      vi.stubGlobal("XMLHttpRequest", ImportRequest);
      const success = vi.spyOn(message, "success").mockImplementation(vi.fn());
      const error = vi.spyOn(message, "error").mockImplementation(vi.fn());
      const onClose = vi.fn();
      const onImported = vi.fn();
      render(
        <ProjectImporter open onClose={onClose} onImported={onImported} />,
      );
      fireEvent.change(screen.getByLabelText("选择项目 ZIP 文件"), {
        target: { files: [new File(["zip"], "backup.zip")] },
      });
      expect(screen.getByText("正在上传文件")).toBeInTheDocument();
      const request = ImportRequest.current;
      act(() => {
        request.upload.onprogress?.(
          new ProgressEvent("progress", {
            lengthComputable,
            loaded: 50,
            total: 100,
          }),
        );
      });
      expect(screen.getByRole("progressbar")).toHaveAttribute(
        "aria-valuenow",
        lengthComputable ? "50" : "0",
      );
      act(() => {
        request.upload.onprogress?.(
          new ProgressEvent("progress", {
            lengthComputable,
            loaded: 100,
            total: 100,
          }),
        );
        request.upload.onload?.();
      });
      expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
      expect(screen.getByRole("status")).toHaveTextContent(
        "文件已上传，正在导入项目",
      );
      expect(screen.queryByText("100%")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "关闭" })).toBeDisabled();
      expect(success).not.toHaveBeenCalled();
      expect(onImported).not.toHaveBeenCalled();

      await act(async () => {
        request.status = outcome === "success" ? 200 : 422;
        request.responseText = JSON.stringify(
          outcome === "success"
            ? { projectId: "project-imported" }
            : { message: "项目文件无法解析" },
        );
        request.onload?.();
      });
      if (outcome === "success") {
        expect(success).toHaveBeenCalled();
        expect(onImported).toHaveBeenCalledOnce();
        expect(onClose).toHaveBeenCalledOnce();
      } else {
        expect(error).toHaveBeenCalledWith("项目文件无法解析", 10);
        expect(onImported).not.toHaveBeenCalled();
        expect(onClose).not.toHaveBeenCalled();
        await waitFor(() =>
          expect(screen.getByRole("button", { name: "关闭" })).toBeEnabled(),
        );
      }
    },
  );
});
