import { creatorApiUrl, hostToken, jsonBody } from "./client";

export interface FeedbackSubmission {
  conversation_id: string;
  assistant_message_id: string;
  score_label: "bad" | "fine" | "good";
  feedback_reason?: string;
  feedback_comment?: string;
}

export interface FeedbackRecord {
  record_type: "feedback";
  timestamp: number;
  project_id: string;
  conversation_id: string;
  assistant_message_id: string;
  score_label: "bad" | "fine" | "good";
  score: number;
  feedback_reason: string;
  feedback_comment: string;
  model_name: string;
  trajectory_span_id?: string;
}

export interface FeedbackResponse {
  ok: boolean;
  record: FeedbackRecord | null;
}

export interface FeedbackReasonsResponse {
  reasons: string[];
}

const headers = (): HeadersInit => {
  const h: HeadersInit = { "Content-Type": "application/json" };
  const token = hostToken();
  if (token) h["Authorization"] = `Bearer ${token}`;
  return h;
};

export async function submitFeedback(
  projectId: string,
  submission: FeedbackSubmission,
): Promise<FeedbackResponse> {
  const response = await fetch(
    creatorApiUrl(`/projects/${projectId}/feedback/`),
    {
      method: "POST",
      headers: headers(),
      body: jsonBody(submission),
    },
  );
  if (!response.ok) {
    const error = await response.text();
    throw new Error(`Failed to submit feedback: ${error}`);
  }
  return response.json();
}

export async function getFeedback(
  projectId: string,
  messageId: string,
): Promise<FeedbackResponse> {
  const response = await fetch(
    creatorApiUrl(
      `/projects/${projectId}/feedback/?message_id=${encodeURIComponent(messageId)}`,
    ),
    {
      method: "GET",
      headers: headers(),
    },
  );
  if (!response.ok) {
    const error = await response.text();
    throw new Error(`Failed to get feedback: ${error}`);
  }
  return response.json();
}

export async function getFeedbackReasons(): Promise<FeedbackReasonsResponse> {
  const response = await fetch(
    creatorApiUrl("/projects/default/feedback/reasons"),
    {
      method: "GET",
      headers: headers(),
    },
  );
  if (!response.ok) {
    // Return default reasons if API fails
    return {
      reasons: [
        "没理解我的意图",
        "任务没有完成",
        "步骤太繁琐",
        "结果有误",
        "回复风格有问题",
        "存在安全风险",
        "响应太慢",
        "其他",
      ],
    };
  }
  return response.json();
}
