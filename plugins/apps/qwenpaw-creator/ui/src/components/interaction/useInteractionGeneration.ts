import { useEffect, useRef, useState } from "react";
import { message } from "antd";
import { dispatchWorkGraphNode } from "@/api/creator/workGraph";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { useCreatorTaskViewStore } from "@/store/creatorTaskViewStore";

export const generationLabels: Record<string, string> = {
  ready: "待生成",
  running: "生成中",
  done: "已生成",
  waiting_review: "待审阅",
  failed: "生成失败",
  gated: "等待前置内容",
};

/** The same persisted-prompt -> explicit generation flow as visual assets. */
export function useInteractionGeneration(
  projectId: string,
  status: string,
  persistedError = "",
) {
  const [busy, setBusy] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [requestError, setRequestError] = useState("");
  const submitting = useRef(false);
  const patch = useProjectSnapshotStore((s) => s.patch);
  const locked = busy || status === "running" || status === "waiting_review";
  useEffect(() => {
    if (["running", "done", "waiting_review"].includes(status)) {
      setRequestError("");
    }
  }, [status]);
  const run = async (operation: () => Promise<unknown>) => {
    if (submitting.current || locked) return;
    submitting.current = true;
    setBusy(true);
    try {
      await operation();
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error));
      throw error;
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };
  return {
    busy,
    generating: generating || status === "running",
    locked,
    error:
      requestError ||
      (!generating && status === "failed"
        ? persistedError || "生成未完成，服务未提供具体原因。请重试。"
        : ""),
    save: (operations: Parameters<typeof patch>[1]) =>
      run(async () => {
        await patch(projectId, operations);
        message.success("提示词已保存，可在右侧重新生成并审阅效果");
      }),
    generate: (nodeId: string) => {
      void run(async () => {
        setRequestError("");
        setGenerating(true);
        try {
          const result = await dispatchWorkGraphNode(projectId, nodeId, {
            regenerate: true,
          });
          if (
            !result.dispatched &&
            !["running", "done"].includes(result.status ?? "")
          ) {
            throw new Error("生成未能启动，请检查前置内容和待审阅任务后重试。");
          }
          message.success("生成已提交，完成后请预览并审阅");
        } catch (error) {
          setRequestError(
            error instanceof Error ? error.message : String(error),
          );
          throw error;
        } finally {
          setGenerating(false);
          void useCreatorTaskViewStore
            .getState()
            .refresh(projectId)
            .catch(() => {});
          void useProjectSnapshotStore
            .getState()
            .pollOnce(projectId)
            .catch(() => {});
        }
      }).catch(() => {});
    },
  };
}
