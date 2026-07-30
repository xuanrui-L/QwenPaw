import { useEffect, useState } from "react";
import { Input, Modal } from "antd";
import type {
  FileProjectReviewRejectionAction,
  FileProjectReviewRejectionFeedback,
} from "@/contracts/creator";

export default function RejectionFeedbackModal({
  open,
  busy,
  targetCount,
  onCancel,
  onSubmit,
}: {
  open: boolean;
  busy: boolean;
  targetCount: number;
  onCancel: () => void;
  onSubmit: (feedback: FileProjectReviewRejectionFeedback) => void;
}) {
  const [problemNote, setProblemNote] = useState("");
  const [regenerationInstruction, setRegenerationInstruction] = useState("");

  useEffect(() => {
    if (!open) return;
    setProblemNote("");
    setRegenerationInstruction("");
  }, [open]);

  const submit = (action: FileProjectReviewRejectionAction) => {
    const normalizedProblem = problemNote.trim();
    const normalizedInstruction = regenerationInstruction.trim();
    onSubmit({
      action,
      ...(normalizedProblem ? { problemNote: normalizedProblem } : {}),
      ...(normalizedInstruction
        ? { regenerationInstruction: normalizedInstruction }
        : {}),
    });
  };

  return (
    <Modal
      title="撤销反馈"
      open={open}
      onCancel={busy ? undefined : onCancel}
      closable={!busy}
      maskClosable={!busy}
      keyboard={!busy}
      destroyOnHidden
      footer={
        <div className="flex justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={onCancel}
            className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-sm text-[var(--color-text-secondary)] disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => submit("UNDO_ONLY")}
            className="rounded-md border border-[var(--color-border)] px-3 py-1.5 text-sm text-[var(--color-text-primary)] disabled:opacity-50"
          >
            仅撤销
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => submit("UNDO_AND_REGENERATE")}
            className="rounded-md bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            撤销并重做
          </button>
        </div>
      }
    >
      <p className="mb-4 text-sm text-[var(--color-text-secondary)]">
        将撤销 {targetCount} 项内容。反馈可选；选择“仅撤销”不会唤醒 Agent。
      </p>
      <div className="space-y-4">
        <label className="block text-sm text-[var(--color-text-primary)]">
          <span className="mb-1 block font-medium">哪里不对（可选）</span>
          <Input.TextArea
            aria-label="哪里不对"
            value={problemNote}
            maxLength={2000}
            showCount
            autoSize={{ minRows: 2, maxRows: 6 }}
            onChange={(event) => setProblemNote(event.target.value)}
            placeholder="例如：人物状态不对，仍然像巅峰时期"
          />
        </label>
        <label className="block text-sm text-[var(--color-text-primary)]">
          <span className="mb-1 block font-medium">重做要求（可选）</span>
          <Input.TextArea
            aria-label="重做要求"
            value={regenerationInstruction}
            maxLength={2000}
            showCount
            autoSize={{ minRows: 2, maxRows: 6 }}
            onChange={(event) => setRegenerationInstruction(event.target.value)}
            placeholder="例如：保留同一角色身份，改为衣衫褴褛、面容憔悴"
          />
        </label>
      </div>
    </Modal>
  );
}
