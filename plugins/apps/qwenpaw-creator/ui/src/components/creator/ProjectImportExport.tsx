import { useCallback, useRef, useState } from "react";
import { Button, Modal, Progress, Tooltip, message } from "antd";
import { Paperclip, X, FileInput } from "lucide-react";
import {
  creatorApiUrl,
  creatorFetch,
  creatorHeaders,
  newClientId,
} from "@/api/creator";
import { useRouter } from "@/routing/navigation";

interface ProjectImportResponse {
  projectId: string;
}

function importProject(
  file: File,
  onProgress?: (uploadedBytes: number, totalBytes: number) => void,
): Promise<ProjectImportResponse> {
  const form = new FormData();
  const requestId = newClientId("import-project");
  form.append("clientRequestId", requestId);
  form.append("file", file, file.name);

  // fetch() has no upload-progress events, so use XMLHttpRequest to report
  // the uploaded byte count to the UI while the request is in flight.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", creatorApiUrl("/projects/import"));
    const headers = creatorHeaders({ "Idempotency-Key": requestId });
    headers.forEach((value, key) => xhr.setRequestHeader(key, value));

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress?.(event.loaded, event.total);
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as ProjectImportResponse);
        } catch {
          reject(new Error("导入失败：无法解析服务端响应"));
        }
        return;
      }
      let msg = `导入失败：HTTP ${xhr.status}`;
      try {
        const body = JSON.parse(xhr.responseText);
        if (body?.message) msg = body.message;
      } catch {
        /* keep default message */
      }
      reject(new Error(msg));
    };
    xhr.onerror = () => reject(new Error("导入失败：网络错误"));
    xhr.onabort = () => reject(new Error("导入失败：已取消"));

    xhr.send(form);
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
  const [uploadedBytes, setUploadedBytes] = useState(0);
  const [totalBytes, setTotalBytes] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const fileSuffix = (file: File) => {
    return file.name.split(".").pop()?.toUpperCase() || undefined;
  };

  const formatBytes = (bytes: number) => {
    if (bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(
      Math.floor(Math.log(bytes) / Math.log(1024)),
      units.length - 1,
    );
    const value = bytes / Math.pow(1024, i);
    return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
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
    setUploadedBytes(0);
    setTotalBytes(attachment.size);
    try {
      const response = await importProject(attachment, (loaded, total) => {
        setUploadedBytes(loaded);
        setTotalBytes(total);
      });
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
                onClick={() => {
                  setAttachment(null);
                  onClose();
                }}
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
                      disabled={uploading}
                      onClick={() => setAttachment(null)}
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

          {attachment && uploading && totalBytes > 0 && (
            <div className="px-4 pb-3">
              <div className="mb-1.5 flex items-center justify-between text-[11px] text-[var(--color-text-secondary)]">
                <span>正在上传…</span>
                <span>
                  {formatBytes(uploadedBytes)} / {formatBytes(totalBytes)}
                  <span className="ml-1 text-[var(--color-text-tertiary)]">
                    ({Math.round((uploadedBytes / totalBytes) * 100)}
                    %)
                  </span>
                </span>
              </div>
              <Progress
                percent={Math.round((uploadedBytes / totalBytes) * 100)}
                size="small"
                status="active"
                showInfo={false}
              />
            </div>
          )}

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-[var(--color-border)] px-3 py-2">
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <Button
                size="small"
                type="text"
                disabled={uploading}
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
