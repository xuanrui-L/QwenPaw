import { CheckCircle2 } from "lucide-react";
import { useExecutionAuthorizationStore } from "@/store/executionAuthorizationStore";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import ExecutionAuthorizationCard from "./ExecutionAuthorizationCard";

export default function AgentDecisionCenter({
  projectId,
}: {
  projectId: string;
}) {
  const project = useProjectSnapshotStore((state) => state.project);
  const storeProjectId = useExecutionAuthorizationStore(
    (state) => state.projectId,
  );
  const authorizations = useExecutionAuthorizationStore((state) => state.items);
  const loading = useExecutionAuthorizationStore((state) => state.loading);
  const error = useExecutionAuthorizationStore((state) => state.error);
  const pending =
    storeProjectId === projectId
      ? authorizations.filter((item) => item.status === "PENDING")
      : [];

  if (loading && pending.length === 0) {
    return (
      <p className="px-1 py-2 text-[11px] text-[var(--color-text-tertiary)]">
        加载生产确认…
      </p>
    );
  }
  if (error && pending.length === 0) {
    return (
      <p className="px-1 py-2 text-[11px] text-[var(--color-danger)]">
        生产确认读取失败：{error}
      </p>
    );
  }
  if (pending.length === 0) {
    return (
      <div className="flex flex-col items-center gap-1.5 py-10 text-center">
        <CheckCircle2 className="h-8 w-8 text-[var(--color-success)]" />
        <p className="text-sm font-medium text-[var(--color-text-primary)]">
          暂无待处理的决策
        </p>
        <p className="text-[11px] text-[var(--color-text-tertiary)]">
          媒体生产需要确认时会显示在这里。
        </p>
      </div>
    );
  }

  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold text-[var(--color-text-tertiary)]">
        <span>生产确认</span>
        <span className="rounded-full bg-[var(--color-bg-secondary)] px-1.5 py-0.5">
          {pending.length}
        </span>
      </div>
      <ul className="space-y-2">
        {pending.map((authorization) => (
          <li key={authorization.id}>
            <ExecutionAuthorizationCard authorization={authorization} project={project} />
          </li>
        ))}
      </ul>
    </div>
  );
}
