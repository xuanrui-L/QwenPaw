import { useState, type ReactNode } from "react";
import type {
  InteractionCreationDocument,
  ProjectDocument,
  TimelineElementDocument,
} from "@/contracts/creator";
import { GenerationPromptEditor } from "@/pages/AssetsPage";
import InteractionView from "./InteractionView";
import DesignViewport from "./DesignViewport";
import {
  generationLabels,
  useInteractionGeneration,
} from "./useInteractionGeneration";

export default function ChoiceEditor({
  project,
  timelineId,
  element,
  status,
  renderPreview,
}: {
  project: ProjectDocument;
  timelineId: string;
  element: TimelineElementDocument;
  status: string;
  renderPreview?: (onSelect: (edgeRef: string) => void) => ReactNode;
}) {
  const creation = element.creation as InteractionCreationDocument;
  const generation = useInteractionGeneration(project.project_id, status);
  const [mobile, setMobile] = useState(false);
  const [selected, setSelected] = useState<string>();
  const selectedIndex = creation.options.findIndex(
    (option) => option.edge_ref === selected,
  );
  const selectedOption = creation.options[selectedIndex];
  const basePath = `/timelines/items/${timelineId}/elements_by_id/${element.element_id}/creation`;
  const savePrompt = (next: string) =>
    generation.save(
      selectedOption
        ? [
            {
              op: "replace",
              path: `${basePath}/options`,
              before: creation.options,
              value: creation.options.map((option) =>
                option.edge_ref === selected
                  ? { ...option, design_prompt: next }
                  : option,
              ),
            },
          ]
        : [
            {
              op: "replace",
              path: `${basePath}/design_prompt`,
              before: creation.design_prompt ?? "",
              value: next,
            },
          ],
    );
  return (
    <article
      data-creator-path={`${basePath}/motion`}
      data-interaction-editor={element.element_id}
    >
      <div className="mb-3 flex items-center justify-between gap-3">
        <strong>
          {project.timelines.items[timelineId].title} · {creation.question}
        </strong>
        <span className="text-xs">{generationLabels[status] ?? status}</span>
      </div>
      <p className="mb-3 text-xs">
        在{" "}
        {(
          element.span.start_tick /
          project.timelines.items[timelineId].ticks_per_second
        ).toFixed(1)}{" "}
        秒暂停并出现抉择。
        {creation.countdown_seconds
          ? `${creation.countdown_seconds} 秒未选择时，前往「${
              project.narrative_edges?.find(
                (edge) => edge.edge_id === creation.default_edge_ref,
              )?.label ?? creation.default_edge_ref
            }」。`
          : "由观众主动选择，不自动跳转。"}
      </p>
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div>
          {renderPreview ? (
            renderPreview(setSelected)
          ) : (
            <>
              <div className="mb-2 flex justify-end gap-1 text-xs">
                {[false, true].map((value) => (
                  <button
                    type="button"
                    key={String(value)}
                    className="btn-secondary"
                    aria-pressed={mobile === value}
                    onClick={() => setMobile(value)}
                  >
                    {value ? "手机" : "桌面"}
                  </button>
                ))}
              </div>
              <DesignViewport mobile={mobile}>
                <InteractionView
                  project={project}
                  creation={creation}
                  countdown={false}
                  onSelect={setSelected}
                />
              </DesignViewport>
              <p className="mt-2 text-xs">
                实际生成的抉择动效 · 视频画面为示意 · 点击选项查看详情
              </p>
            </>
          )}
        </div>
        <aside
          className="min-w-0 space-y-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-primary)] p-4"
          data-interaction-details
        >
          <strong className="text-sm">交互按钮与剧情走向</strong>
          <div className="grid gap-2">
            <button
              type="button"
              className="btn-secondary"
              aria-pressed={!selectedOption}
              onClick={() => setSelected(undefined)}
            >
              整体抉择设计
            </button>
            {creation.options.map((option) => {
              const edge = project.narrative_edges?.find(
                (value) => value.edge_id === option.edge_ref,
              );
              return (
                <button
                  type="button"
                  className="btn-secondary"
                  key={option.edge_ref}
                  aria-pressed={selected === option.edge_ref}
                  onClick={() => setSelected(option.edge_ref)}
                >
                  {edge?.label ?? option.edge_ref} →{" "}
                  {edge
                    ? project.timelines.items[edge.target_timeline_id]?.title
                    : "未知节点"}
                </button>
              );
            })}
          </div>
          <GenerationPromptEditor
            key={selected ?? "whole"}
            target={{
              pointer: selectedOption
                ? `${basePath}/options/${selectedIndex}/design_prompt`
                : `${basePath}/design_prompt`,
              label: selectedOption ? "按钮外观与动效提示词" : "抉择生成提示词",
              value:
                (selectedOption
                  ? selectedOption.design_prompt
                  : creation.design_prompt) ?? "",
            }}
            saving={generation.locked}
            regenerateLabel={
              generation.busy || status === "running"
                ? "生成中…"
                : creation.motion
                ? "重新生成抉择"
                : "生成抉择动效"
            }
            onSave={(_, next) => savePrompt(next)}
            onRegenerate={() =>
              generation.generate(`interaction:${element.element_id}`)
            }
          />
          <p className="text-xs text-[var(--color-text-secondary)]">
            编辑完成后保存提示词；重新生成更新此处抉择，完成后进入审阅。按钮文案与走向来自剧本分支，可在蓝图或通过助手修改。
          </p>
          {status === "waiting_review" && (
            <p className="text-xs">请先在审阅面板批准或拒绝本次生成。</p>
          )}
        </aside>
      </div>
    </article>
  );
}
