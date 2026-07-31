import { CircleAlert, CircleCheck } from "lucide-react";
import type {
  VisualCoverageReport,
  VisualCoverageStatus,
} from "@/selectors/visualVariantCoverage";
import { visualVariantLabel } from "@/lib/visualVariants";

const STATUS_LABEL: Record<VisualCoverageStatus, string> = {
  covered: "覆盖完成",
  missing_variant: "尚未定义 Variant",
  unassigned_variant: "Element 未绑定 Variant",
  missing_artifact: "Variant 尚无使用中产物",
};

function kindLabel(kind: "character" | "scene" | "prop"): string {
  if (kind === "character") return "角色";
  if (kind === "scene") return "场景";
  return "道具";
}

export default function VisualCoverageCheckpoint({
  report,
}: {
  report: VisualCoverageReport;
}) {
  if (!report.total) return null;
  return (
    <details
      data-visual-coverage-checkpoint
      className={`shrink-0 border-b px-5 py-2 ${
        report.issueCount
          ? "border-amber-200 bg-amber-50/80"
          : "border-emerald-200 bg-emerald-50/70"
      }`}
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 text-xs font-semibold text-[var(--color-text-primary)]">
        {report.issueCount ? (
          <CircleAlert className="h-4 w-4 text-amber-600" />
        ) : (
          <CircleCheck className="h-4 w-4 text-emerald-600" />
        )}
        视觉覆盖 {report.covered}/{report.total}
        <span className="font-normal text-[var(--color-text-secondary)]">
          {report.issueCount
            ? `· ${report.issueCount} 项需要核对`
            : "· 所有被引用实体均已绑定产物"}
        </span>
      </summary>
      <div className="mt-2 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {report.items.map((item) => (
          <div
            key={item.entity.entity_id}
            className="rounded-lg border border-black/5 bg-white/80 p-2.5 text-[11px]"
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate font-semibold text-[var(--color-text-primary)]">
                  {item.entity.name}
                </div>
                <div className="text-[var(--color-text-tertiary)]">
                  {kindLabel(item.entity.kind)} ·{" "}
                  {item.referencedElementIds.length} 个 Element 引用
                </div>
              </div>
              <span
                className={`shrink-0 rounded-full px-2 py-0.5 ${
                  item.status === "covered"
                    ? "bg-emerald-100 text-emerald-700"
                    : "bg-amber-100 text-amber-700"
                }`}
              >
                {STATUS_LABEL[item.status]}
              </span>
            </div>
            {item.unassignedElementIds.length > 0 && (
              <div className="mt-2 rounded bg-amber-50 px-2 py-1 text-amber-700">
                {item.unassignedElementIds.length} 个 Element 未指定 Variant
              </div>
            )}
            {item.variants.length > 0 && (
              <div className="mt-2 space-y-1">
                {item.variants.map((variant) => (
                  <div
                    key={variant.variant.variant_id}
                    className="flex items-center justify-between gap-2 text-[var(--color-text-secondary)]"
                  >
                    <span className="min-w-0 truncate">
                      {visualVariantLabel(variant.variant)} ·{" "}
                      {variant.referencedElementIds.length} 个 Element
                    </span>
                    <span
                      className={
                        variant.selectedAvailable
                          ? "text-emerald-700"
                          : "text-amber-700"
                      }
                    >
                      {variant.selectedAvailable
                        ? `使用中 · ${variant.generatedCount} 个产物`
                        : `${variant.generatedCount} 个产物 · 未选择`}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}
