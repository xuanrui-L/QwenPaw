import { useState } from "react";
import { message } from "antd";
import { Coins, PlayCircle } from "lucide-react";
import type {
  ExecutionAuthorizationApproval,
  ExecutionAuthorizationView,
} from "@/contracts/creator";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { creatorTargetLabel, taskKindLabel } from "@/lib/creatorPresentation";

const BUTTON_BASE =
  "rounded-md px-2 py-1 text-[11px] font-medium transition-colors disabled:opacity-50";
const BUTTON_PRIMARY = `${BUTTON_BASE} bg-[var(--color-accent)] text-white hover:opacity-90`;
const BUTTON_GHOST = `${BUTTON_BASE} border border-[var(--color-border)] bg-[var(--color-bg-primary)] text-[var(--color-text-secondary)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent)]`;

export interface AuthorizationBilling {
  estimatedCost?: number | null;
  currency?: string;
  displayText?: string;
  unitPrice?: string;
  formula?: string;
  pricingModel?: string;
  pricingSource?: string;
  approximate?: boolean;
  notes?: string[];
}

export function authorizationApprovalPayload(
  authorization: ExecutionAuthorizationView,
): ExecutionAuthorizationApproval {
  return {
    authorizationToken: authorization.authorizationToken,
    provider: authorization.provider,
    model: authorization.model,
    maxCost: authorization.estimatedCost ?? 0,
    maxCandidates: authorization.maxCandidates,
  };
}

export function authorizationBilling(
  authorization: ExecutionAuthorizationView,
): AuthorizationBilling | null {
  const billing = authorization.scope.billing;
  if (!billing || typeof billing !== "object") return null;
  return billing as AuthorizationBilling;
}

function authorizationOperation(
  authorization: ExecutionAuthorizationView,
): string {
  return typeof authorization.scope.operation === "string"
    ? taskKindLabel(authorization.scope.operation)
    : "高成本媒体执行";
}

function authorizationParameterSummary(
  authorization: ExecutionAuthorizationView,
): string {
  const raw = authorization.scope.parameters;
  if (!raw || typeof raw !== "object") return "";
  const parameters = raw as Record<string, unknown>;
  const parts: string[] = [];
  if (parameters.durationSeconds) {
    parts.push(`时长 ${parameters.durationSeconds}秒`);
  }
  if (typeof parameters.resolution === "string") {
    parts.push(`分辨率 ${parameters.resolution.toUpperCase()}`);
  }
  if (typeof parameters.ratio === "string") {
    parts.push(`比例 ${parameters.ratio}`);
  }
  if (typeof parameters.aspectRatio === "string") {
    parts.push(`画幅 ${parameters.aspectRatio}`);
  }
  if (typeof parameters.generateAudio === "boolean") {
    parts.push(parameters.generateAudio ? "有声" : "无声");
  }
  return parts.join(" · ");
}

export function authorizationDetail(
  authorization: ExecutionAuthorizationView,
): string {
  const messageText = authorization.scope.message;
  if (typeof messageText === "string" && messageText.trim()) return messageText;
  const detail = `${authorizationOperation(authorization)} · ${creatorTargetLabel(
    authorization.targetRef,
  )} · ${authorization.provider}/${authorization.model}`;
  const billing = authorizationBilling(authorization);
  return billing?.displayText
    ? `${detail} · 预计费用 ${billing.displayText}`
    : detail;
}

export default function ExecutionAuthorizationCard({
  authorization,
}: {
  authorization: ExecutionAuthorizationView;
}) {
  const approve = useExecutionAuthorizationStore((state) => state.approve);
  const decline = useExecutionAuthorizationStore((state) => state.decline);
  const [busy, setBusy] = useState(false);
  if (authorization.status !== "PENDING") return null;

  const billing = authorizationBilling(authorization);
  const parameterSummary = authorizationParameterSummary(authorization);

  const continueRun = async () => {
    setBusy(true);
    try {
      await approve(
        authorization.id,
        authorizationApprovalPayload(authorization),
      );
      message.success("已确认，专业制作将继续");
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const cancelRun = async () => {
    setBusy(true);
    try {
      await decline(authorization.id, authorization.authorizationToken);
      message.success("已取消，当前制作已终止");
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <article
      data-execution-authorization-card={authorization.id}
      className="rounded-xl border border-[var(--color-warning)]/50 bg-[var(--color-warning-soft)]/40 p-2.5"
    >
      <div className="flex items-start gap-2.5">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="inline-flex items-center gap-1 rounded bg-[var(--color-warning-soft)] px-1.5 py-0.5 text-[9px] font-bold text-[var(--color-warning)]">
              <PlayCircle className="h-3 w-3" />
              生产确认
            </span>
            <span className="min-w-0 flex-1 truncate text-xs font-semibold text-[var(--color-text-primary)]">
              {authorizationOperation(authorization)}等待确认
            </span>
          </div>
          <dl className="mt-1.5 space-y-0.5 text-[11px] leading-4">
            <div className="flex gap-1">
              <dt className="shrink-0 text-[var(--color-text-tertiary)]">
                对象
              </dt>
              <dd className="min-w-0 truncate text-[var(--color-text-secondary)]">
                {creatorTargetLabel(authorization.targetRef)}
              </dd>
            </div>
            <div className="flex gap-1">
              <dt className="shrink-0 text-[var(--color-text-tertiary)]">
                模型
              </dt>
              <dd className="min-w-0 truncate text-[var(--color-text-secondary)]">
                {authorization.provider} / {authorization.model}
              </dd>
            </div>
            {parameterSummary && (
              <div className="flex gap-1">
                <dt className="shrink-0 text-[var(--color-text-tertiary)]">
                  参数
                </dt>
                <dd className="min-w-0 text-[var(--color-text-secondary)]">
                  {parameterSummary}
                </dd>
              </div>
            )}
          </dl>
          {billing ? (
            <div className="mt-1.5 rounded-md border border-[var(--color-warning)]/30 bg-[var(--color-bg-primary)]/60 px-2 py-1.5">
              <p className="flex items-center gap-1 text-[11px] font-semibold leading-4 text-[var(--color-warning)]">
                <Coins className="h-3 w-3" />
                预计费用 {billing.displayText ?? "费用未知"}
              </p>
              {billing.formula && (
                <p className="mt-0.5 text-[10px] leading-4 text-[var(--color-text-secondary)]">
                  计算：{billing.formula}
                  {billing.pricingModel ? `（按 ${billing.pricingModel} 计价）` : ""}
                </p>
              )}
              {(billing.notes ?? []).map((note) => (
                <p
                  key={note}
                  className="text-[10px] leading-4 text-[var(--color-text-tertiary)]"
                >
                  {note}
                </p>
              ))}
            </div>
          ) : (
            <p className="mt-0.5 line-clamp-2 text-[11px] leading-4 text-[var(--color-text-tertiary)]">
              {authorizationDetail(authorization)}
            </p>
          )}
        </div>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <button
          type="button"
          disabled={busy}
          onClick={() => void continueRun()}
          className={`flex-1 ${BUTTON_PRIMARY}`}
        >
          继续
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => void cancelRun()}
          className={`flex-1 ${BUTTON_GHOST}`}
        >
          取消
        </button>
      </div>
    </article>
  );
}
