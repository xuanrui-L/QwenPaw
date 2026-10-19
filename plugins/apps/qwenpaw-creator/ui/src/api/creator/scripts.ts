import type { TimelineScriptImportResult } from "@/contracts/creator";
import { creatorRequest } from "./client";

const scriptImportPath = (projectId: string, timelineId: string) =>
  `/projects/${encodeURIComponent(projectId)}/timelines/${encodeURIComponent(
    timelineId,
  )}/script-import`;

/**
 * 剧本直登：把用户写好的剧本正文（UTF-8 文本）直接登记为该集当前剧本版本。
 *
 * 服务端读取正文并落 `timeline_script` 版本，不创建 ProjectSource，因此不会
 * 唤醒素材理解链路；重复提交同一内容不会新增版本。
 */
export function importTimelineScript(
  projectId: string,
  timelineId: string,
  input: { file: File } | { content: string; name?: string },
): Promise<TimelineScriptImportResult> {
  if ("file" in input) {
    const form = new FormData();
    form.append("file", input.file, input.file.name);
    return creatorRequest(scriptImportPath(projectId, timelineId), {
      method: "POST",
      body: form,
    });
  }
  return creatorRequest(scriptImportPath(projectId, timelineId), {
    method: "POST",
    body: JSON.stringify({
      content: input.content,
      name: input.name ?? "",
    }),
  });
}
