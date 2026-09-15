import { useRef, useState } from "react";
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
export function useInteractionGeneration(projectId: string, status: string) {
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const patch = useProjectSnapshotStore((s) => s.patch);
  const locked = busy || status === "running" || status === "waiting_review";
  const run = async (operation: () => Promise<unknown>) => {
    if (submitting.current || locked) return;
    submitting.current = true;
    setBusy(true);
    try {
      await operation();
    } catch (error) {
      message.error((error as Error).message);
      throw error;
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };
  return {
    busy,
    locked,
    save: (operations: Parameters<typeof patch>[1]) =>
      run(async () => {
        await patch(projectId, operations);
        message.success("提示词已保存，可在右侧重新生成并审阅效果");
      }),
    generate: (nodeId: string) => {
      void run(async () => {
        await dispatchWorkGraphNode(projectId, nodeId, { regenerate: true });
        message.success("生成已提交，完成后请预览并审阅");
        void useCreatorTaskViewStore.getState().refresh(projectId);
        void useProjectSnapshotStore.getState().pollOnce(projectId);
      }).catch(() => {});
    },
  };
}
