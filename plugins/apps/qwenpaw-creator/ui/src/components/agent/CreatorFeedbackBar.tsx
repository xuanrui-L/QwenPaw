import { useState } from "react";
import { Button, Checkbox, Input, Modal, message as antMessage, Tooltip } from "antd";
import {
  DislikeOutlined,
  LikeOutlined,
  MehOutlined,
} from "@ant-design/icons";
import {
  submitFeedback,
  getFeedbackReasons,
  type FeedbackSubmission,
} from "@/api/creator/feedback";
import type { CreatorMessage } from "@/contracts/creator";
import { useParams } from "@/routing/navigation";

const { TextArea } = Input;

const SCORE_LABELS = {
  bad: { label: "糟糕", icon: <DislikeOutlined />, color: "#ff4d4f" },
  fine: { label: "一般", icon: <MehOutlined />, color: "#faad14" },
  good: { label: "优秀", icon: <LikeOutlined />, color: "#52c41a" },
} as const;

type ScoreLabel = keyof typeof SCORE_LABELS;

interface CreatorFeedbackBarProps {
  message: CreatorMessage;
}

export default function CreatorFeedbackBar({
  message,
}: CreatorFeedbackBarProps) {
  const params = useParams();
  const projectId = (params as { projectId?: string }).projectId || "default";

  const [selectedScore, setSelectedScore] = useState<ScoreLabel | null>(null);
  const [showReasonModal, setShowReasonModal] = useState(false);
  const [selectedReasons, setSelectedReasons] = useState<string[]>([]);
  const [comment, setComment] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [reasons, setReasons] = useState<string[]>([]);

  const handleSubmit = async (scoreLabel: ScoreLabel) => {
    if (scoreLabel === "bad") {
      // Load reasons and show modal
      const reasonsData = await getFeedbackReasons();
      setReasons(reasonsData.reasons);
      setSelectedScore(scoreLabel);
      setShowReasonModal(true);
      return;
    }

    await doSubmit(scoreLabel, "", "");
  };

  const doSubmit = async (
    scoreLabel: ScoreLabel,
    feedbackReason: string,
    feedbackComment: string,
  ) => {
    setSubmitting(true);
    try {
      const submission: FeedbackSubmission = {
        conversation_id: (message.metadata?.conversationId as string) || "",
        assistant_message_id: message.messageId,
        score_label: scoreLabel,
        feedback_reason: feedbackReason,
        feedback_comment: feedbackComment,
      };

      await submitFeedback(projectId, submission);
      setSubmitted(true);
      antMessage.success(`已反馈：${SCORE_LABELS[scoreLabel].label}`);
    } catch (error) {
      console.error("Failed to submit feedback:", error);
      antMessage.error("反馈提交失败，请重试");
    } finally {
      setSubmitting(false);
      setShowReasonModal(false);
    }
  };

  const handleReasonModalOk = () => {
    if (selectedReasons.length === 0) {
      antMessage.warning("请至少选择一个原因");
      return;
    }
    doSubmit("bad", selectedReasons.join("；"), comment);
  };

  if (submitted) {
    return (
      <div className="mt-2 flex items-center gap-2 text-xs text-gray-500">
        <span>已反馈</span>
      </div>
    );
  }

  return (
    <>
      <div className="mt-2 flex items-center gap-2">
        <span className="text-xs text-gray-500">这个回答对你有帮助吗？</span>
        {(Object.keys(SCORE_LABELS) as ScoreLabel[]).map((key) => {
          const { label, icon } = SCORE_LABELS[key];
          return (
            <Tooltip key={key} title={label}>
              <Button
                type="text"
                size="small"
                icon={icon}
                loading={submitting && selectedScore === key}
                onClick={() => handleSubmit(key)}
                className="text-gray-500 hover:text-gray-700"
              />
            </Tooltip>
          );
        })}
      </div>

      <Modal
        title="请告诉我们哪里不好"
        open={showReasonModal}
        onOk={handleReasonModalOk}
        onCancel={() => setShowReasonModal(false)}
        confirmLoading={submitting}
        okText="提交"
        cancelText="取消"
      >
        <div className="mb-4">
          <p className="mb-2 text-sm text-gray-600">请选择主要原因：</p>
          <Checkbox.Group
            value={selectedReasons}
            onChange={(values) => setSelectedReasons(values as string[])}
            className="flex flex-col gap-2"
          >
            {reasons.map((reason) => (
              <Checkbox key={reason} value={reason}>
                {reason}
              </Checkbox>
            ))}
          </Checkbox.Group>
        </div>
        <div>
          <p className="mb-2 text-sm text-gray-600">补充说明（可选）：</p>
          <TextArea
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="请描述具体问题..."
            rows={3}
            maxLength={500}
            showCount
          />
        </div>
      </Modal>
    </>
  );
}
