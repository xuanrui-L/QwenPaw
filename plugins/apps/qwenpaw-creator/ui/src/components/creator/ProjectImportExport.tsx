import { creatorFetch, newClientId } from "@/api/creator/client";

export async function saveExportFile(projectId: string) {
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

  const blob = await response.blob();

  // 优先从 Content-Disposition 取后端给定的文件名，否则回退到 projectId.zip。
  let filename = `${projectId}.zip`;
  const disposition = response.headers.get("Content-Disposition");
  if (disposition) {
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    if (match?.[1]) {
      filename = decodeURIComponent(match[1]);
    }
  }
  const dot = filename.lastIndexOf(".");
  const baseName = dot > 0 ? filename.slice(0, dot) : filename;
  const ext = dot > 0 ? filename.slice(dot + 1) : "zip";

  // 优先用 File System Access API 的 showSaveFilePicker 弹出系统保存对话框，
  // 让用户选择保存路径与文件名；不支持时回退到 <a download> 静默下载。
  const picker =
    (window as unknown as {
      showSaveFilePicker?: (opts: unknown) => Promise<FileSystemFileHandle>;
    }).showSaveFilePicker;

  if (typeof picker === "function") {
    let handle: FileSystemFileHandle;
    try {
      handle = await picker({
        suggestedName: filename,
        types: [
          {
            description: "项目导出文件",
            accept: { "application/zip": [`.${ext}`] },
          },
        ],
      });
    } catch (err) {
      // 用户在保存对话框点了取消 —— 不当作错误。
      if (err instanceof DOMException && err.name === "AbortError") return;
      throw err;
    }
    const writable = await handle.createWritable();
    try {
      await writable.write(blob);
    } finally {
      await writable.close();
    }
    return;
  }

  // 回退：不支持 showSaveFilePicker 的浏览器，用 anchor 触发下载。
  void baseName; // baseName 暂未在回退路径使用，保留以便后续命名定制。
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
