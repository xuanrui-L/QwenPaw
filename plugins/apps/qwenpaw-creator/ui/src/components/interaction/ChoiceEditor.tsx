import { useState } from "react";
import { message } from "antd";
import type {
  InteractionCreationDocument,
  ProjectDocument,
  TimelineElementDocument,
} from "@/contracts/creator";
import { dispatchWorkGraphNode } from "@/api/creator/workGraph";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import InteractionView from "./InteractionView";
import DesignViewport from "./DesignViewport";
import { generationLabels } from "./PresentationEditor";
import { useDesignDraft } from "./useDesignDraft";

export default function ChoiceEditor({
  project,
  timelineId,
  element,
  status,
}: {
  project: ProjectDocument;
  timelineId: string;
  element: TimelineElementDocument;
  status: string;
}) {
  const creation = element.creation as InteractionCreationDocument;
  const initial = JSON.stringify({
    design_prompt: creation.design_prompt ?? "",
    options: creation.options,
  });
  const draft = useDesignDraft(initial);
  const design = JSON.parse(draft.value) as Pick<
    InteractionCreationDocument,
    "design_prompt" | "options"
  >;
  const [busy, setBusy] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [selected, setSelected] = useState(creation.options[0]?.edge_ref);
  const patch = useProjectSnapshotStore((s) => s.patch);
  const basePath = `/timelines/items/${timelineId}/elements_by_id/${element.element_id}/creation`;
  const generate = async () => {
    setBusy(true);
    try {
      if (draft.dirty) {
        const base = JSON.parse(draft.base);
        await patch(project.project_id, [
          {
            op: "replace",
            path: `${basePath}/design_prompt`,
            before: base.design_prompt,
            value: design.design_prompt,
          },
          {
            op: "replace",
            path: `${basePath}/options`,
            before: base.options,
            value: design.options,
          },
        ]);
        draft.saved();
      }
      const result = await dispatchWorkGraphNode(
        project.project_id,
        `interaction:${element.element_id}`,
      );
      message.info(
        result.status === "done"
          ? "当前抉择已生成，修改设计后可重新生成"
          : "抉择生成已提交，完成后请预览并审阅",
      );
    } catch (error) {
      message.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const selectedOption = design.options.find(
    (option) => option.edge_ref === selected,
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
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_280px]">
        <div>
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
            实际生成的抉择动效 · 点击选项可定位设计 · 预览中倒计时不推进
          </p>
        </div>
        <div className="rounded-xl border border-[var(--color-border)] p-3">
          <label className="block text-xs">
            抉择布局与动效要求
            <textarea
              aria-label="抉择布局与动效要求"
              className="mt-1 w-full rounded border bg-[var(--color-bg-secondary)] p-2"
              rows={4}
              value={design.design_prompt}
              onChange={(e) =>
                draft.change(
                  JSON.stringify({ ...design, design_prompt: e.target.value }),
                )
              }
            />
          </label>
          <p className="my-2 text-xs font-medium">交互按钮与剧情走向</p>
          <div className="grid gap-2">
            {design.options.map((option) => {
              const edge = project.narrative_edges?.find(
                (value) => value.edge_id === option.edge_ref,
              );
              return (
                <button
                  type="button"
                  className={
                    selected === option.edge_ref
                      ? "btn-primary"
                      : "btn-secondary"
                  }
                  key={option.edge_ref}
                  onClick={() => setSelected(option.edge_ref)}
                  aria-pressed={selected === option.edge_ref}
                >
                  {edge?.label ?? option.edge_ref} →{" "}
                  {edge
                    ? project.timelines.items[edge.target_timeline_id]?.title
                    : "未知节点"}
                </button>
              );
            })}
          </div>
          {selectedOption && (
            <label className="mt-3 block text-xs">
              所选交互按钮的外观与动效
              <textarea
                aria-label="所选交互按钮的外观与动效"
                className="mt-1 w-full rounded border bg-[var(--color-bg-secondary)] p-2"
                rows={4}
                value={selectedOption.design_prompt ?? ""}
                onChange={(e) =>
                  draft.change(
                    JSON.stringify({
                      ...design,
                      options: design.options.map((option) =>
                        option.edge_ref === selected
                          ? { ...option, design_prompt: e.target.value }
                          : option,
                      ),
                    }),
                  )
                }
              />
            </label>
          )}
          <p className="mt-2 text-xs text-[var(--color-text-secondary)]">
            按钮文案与走向来自剧本分支，可在蓝图中修改，或告诉助手需要调整的剧情。
          </p>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-primary"
          disabled={
            busy ||
            draft.conflict ||
            status === "running" ||
            status === "waiting_review"
          }
          onClick={() => void generate()}
        >
          {busy ? "提交中…" : draft.dirty ? "保存并生成抉择" : "生成抉择动效"}
        </button>
        {draft.dirty && (
          <button type="button" className="btn-secondary" onClick={draft.reset}>
            撤销未保存修改
          </button>
        )}
        <span className="text-xs">
          {draft.dirty
            ? "有未保存修改，预览仍为上次生成结果。"
            : "只更新此处的抉择界面，不重做视频。"}
        </span>
      </div>
      {draft.conflict && (
        <p role="alert">
          设计已被其他操作更新。请先复制未保存内容，再撤销修改以载入最新设计。
        </p>
      )}
      {status === "waiting_review" && (
        <p className="mt-2 text-xs">
          请在审阅面板批准或拒绝本次生成，再继续修改。
        </p>
      )}
    </article>
  );
}
