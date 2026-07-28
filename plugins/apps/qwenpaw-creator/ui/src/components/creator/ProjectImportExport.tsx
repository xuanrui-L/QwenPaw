import { useCallback, useRef, useState } from "react";
import { Button, Modal, Tooltip, message } from "antd";
import { Paperclip, X, FileInput } from "lucide-react";
import { creatorFetch, newClientId, creatorRequest } from "@/api/creator";
import { useRouter } from "@/routing/navigation";

interface ProjectImportResponse {
  projectId: string;
}

function importProject(file: File): Promise<ProjectImportResponse> {
  const form = new FormData();
  const requestId = newClientId("import-project");
  form.append("clientRequestId", requestId);
  form.append("file", file, file.name);
  return creatorRequest("/projects/import", {
    method: "POST",
    headers: { "Idempotency-Key": requestId },
    body: form,
  });
}

interface ProjectImporterProps {
  open: boolean;
  onClose: () => void;
  onImported?: () => void;
}

export function ProjectImporter({
  open,
  onClose,
  onImported,
}: ProjectImporterProps) {
  const router = useRouter();
  const [attachment, setAttachment] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fileSuffix = (file: File) => {
    return file.name.split(".").pop()?.toUpperCase() || undefined;
  };

  const addFile = useCallback((selectedFile: File) => {
    if (!selectedFile) {
      message.error("没有选择任何文件");
      return;
    }

    if (fileSuffix(selectedFile) !== "ZIP") {
      message.error("只接受ZIP文件");
      return;
    }

    setAttachment(selectedFile);
  }, []);

  const removeAttachment = () => {
    setAttachment(null);
  };

  const canUpload = !uploading && attachment !== null;

  const uploadHint = () => {
    if (!attachment) return "请选择一个项目zip文件";
    return undefined;
  };

  const handleUpload = async () => {
    if (!attachment) {
      message.error("请选择一个项目zip文件");
      return;
    }
    setUploading(true);
    try {
      const response = await importProject(attachment);
      message.success("成功导入项目：" + response.projectId, 10);
      console.log("import response:" + JSON.stringify(response));
      setAttachment(null);
      onClose();
      onImported?.();
    } catch (error) {
      message.error(
        "导入失败" + (error instanceof Error ? ": " + error.message : ""),
        10,
      );
      console.log(error instanceof Error ? error.message : "导入失败");
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  return (
    <Modal
      open={open}
      onCancel={uploading ? undefined : onClose}
      footer={null}
      width={720}
      centered
      closable={false}
      destroyOnHidden
      title={null}
    >
      <div className="pt-2">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 className="flex items-center gap-2 text-lg font-semibold text-[var(--color-text-primary)]">
              <FileInput className="h-5 w-5 text-[var(--color-accent)]" />
              导入已有项目
            </h2>
            <p className="mt-1 text-xs text-[var(--color-text-secondary)]">
              上传项目zip文件，在系统里恢复成可操作项目。
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {!uploading && (
              <button
                type="button"
                onClick={onClose}
                className="flex h-7 w-7 items-center justify-center rounded-lg text-[var(--color-text-tertiary)] transition-colors hover:bg-[var(--color-bg-secondary)] hover:text-[var(--color-text-primary)]"
                aria-label="关闭"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>

        <div
          className={`mt-3 rounded-xl border-2 transition-colors border-[var(--color-border)]`}
        >
          {attachment && (
            <div className="flex flex-wrap gap-1.5 px-4 pb-2">
              {(() => {
                return (
                  <span
                    key={attachment.name}
                    className="inline-flex items-center gap-1.5 rounded-full border border-[var(--color-border)] bg-[var(--color-bg-secondary)] py-1 pl-2 pr-1 text-[11px] text-[var(--color-text-secondary)]"
                  >
                    <b className="shrink-0 text-[10px] text-[var(--color-accent)]">
                      {fileSuffix(attachment)}
                    </b>

                    {attachment.name}
                    <button
                      type="button"
                      onClick={() => removeAttachment()}
                      className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full hover:bg-[var(--color-border)]"
                      aria-label="移除附件"
                    >
                      <X className="h-2.5 w-2.5" />
                    </button>
                  </span>
                );
              })()}
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--color-border)] px-3 py-2">
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <Button
                size="small"
                type="text"
                icon={<Paperclip className="h-3.5 w-3.5" />}
                onClick={() => fileInputRef.current?.click()}
                className="!text-xs !text-[var(--color-text-secondary)]"
              >
                选择项目zip文件
              </Button>
              <input
                ref={fileInputRef}
                type="file"
                hidden
                onChange={(e) => {
                  const selectedFile = e.target.files?.[0];
                  if (selectedFile) addFile(selectedFile);
                  e.target.value = "";
                }}
              />
            </div>
            <Tooltip title={canUpload ? undefined : uploadHint()}>
              <Button
                type="primary"
                icon={<FileInput className="h-3.5 w-3.5" />}
                disabled={!canUpload}
                loading={uploading}
                onClick={handleUpload}
                className="!flex !items-center !gap-1.5 !font-semibold"
              >
                导入
              </Button>
            </Tooltip>
          </div>
        </div>
      </div>
    </Modal>
  );
}

export async function saveExportFile(
  projectId: string,
  onChunk?: (byteLength: number) => void,
) {
  const response = await creatorFetch(
    `/projects/${encodeURIComponent(projectId)}/export`,
    {
      method: "GET",
      headers: { "Idempotency-Key": newClientId("export-project") },
    },
  );
  if (!response.ok) {
    throw new Error(`导出失败：HTTP ${response.status}`);
  }
  if (!response.body) {
    throw new Error("导出失败：没有数据");
  }

  let filename = `${projectId}.zip`;
  const disposition = response.headers.get("Content-Disposition");
  if (disposition) {
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    if (match?.[1]) {
      filename = decodeURIComponent(match[1]);
    }
  }

  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    onChunk?.(value.byteLength);
  }

  const blob = new Blob(chunks);

  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
