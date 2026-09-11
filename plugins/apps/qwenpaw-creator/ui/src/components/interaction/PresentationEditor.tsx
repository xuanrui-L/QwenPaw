import { useEffect, useState } from "react";
import { selectLiveTimelineIds } from "@/selectors/timelineElementSelectors";
import ChoiceEditor from "./ChoiceEditor";
import type {
  InteractiveScreenDesignDocument,
  PresentationActionId,
  PresentationScreenId,
  ProjectDocument,
} from "@/contracts/creator";
import { GenerationPromptEditor } from "@/pages/AssetsPage";
import { actions, screens, type PreviewControl } from "./presentationDesign";
import { PresentationPreview } from "./PresentationPreview";
import {
  generationLabels,
  useInteractionGeneration,
} from "./useInteractionGeneration";

export { PresentationPreview } from "./PresentationPreview";

export default function PresentationEditor({
  project,
  status = "",
  statuses = {},
  focusField = "",
}: {
  project: ProjectDocument;
  status?: string;
  statuses?: Record<string, string>;
  focusField?: string;
}) {
  const design = project.interactive_presentation;
  const generation = useInteractionGeneration(project.project_id, status);
  const [selection, setSelection] = useState<{
    screen: PresentationScreenId;
    action?: PresentationActionId;
  }>({ screen: "title" });
  const [controls, setControls] = useState<PreviewControl[]>([]);
  const [choiceId, setChoiceId] = useState("");
  const points = selectLiveTimelineIds(project).flatMap((tid) =>
    Object.values(project.timelines.items[tid].elements_by_id)
      .filter(
        (element) => element.enabled && element.creation.type === "interaction",
      )
      .map((element) => ({ tid, element })),
  );
  const point =
    selection.screen === "play"
      ? points.find((item) => item.element.element_id === choiceId)
      : undefined;
  useEffect(() => {
    const focused = points.find((item) =>
      focusField.includes(
        `/elements_by_id/${item.element.element_id}/creation`,
      ),
    );
    if (focused) {
      setChoiceId(focused.element.element_id);
      setSelection({ screen: "play" });
    }
  }, [focusField]);
  const page = screens.find((item) => item.id === selection.screen)!;
  const pageDesign = design?.screens?.[page.id];
  const buttonIds = [
    ...new Set([
      ...page.required,
      ...Object.keys(pageDesign?.controls ?? {}),
      ...controls
        .filter((control) => control.screen === page.id)
        .map((control) => control.action),
    ]),
  ] as PresentationActionId[];
  const updateScreen = (value: Partial<InteractiveScreenDesignDocument>) =>
    generation.save([
      {
        op: design?.screens ? "replace" : "add",
        path: "/interactive_presentation/screens",
        before: design?.screens,
        missingBefore: !design?.screens,
        value: {
          ...design?.screens,
          [page.id]: {
            design_prompt: "",
            controls: {},
            ...pageDesign,
            ...value,
          },
        },
      },
    ]);
  const chosen = selection.action
    ? pageDesign?.controls?.[selection.action]
    : undefined;
  const actual = controls.find(
    (control) =>
      control.screen === page.id && control.action === selection.action,
  );
  const updateControl = (value: { label?: string; design_prompt?: string }) =>
    updateScreen({
      controls: {
        ...pageDesign?.controls,
        [selection.action!]: {
          label: "",
          design_prompt: "",
          ...chosen,
          ...value,
        },
      },
    });
  const pagePath = `/interactive_presentation/screens/${page.id}`;
  const promptPath = selection.action
    ? `${pagePath}/controls/${selection.action}/design_prompt`
    : `${pagePath}/design_prompt`;
  return (
    <article
      data-creator-path="/interactive_presentation/motion"
      data-presentation-editor
    >
      <div
        className="my-3 flex flex-wrap gap-2"
        role="tablist"
        aria-label="作品页面"
      >
        {screens.map((item) => (
          <button
            type="button"
            role="tab"
            aria-selected={page.id === item.id}
            className="btn-secondary"
            key={item.id}
            onClick={() => setSelection({ screen: item.id })}
          >
            {item.label}
          </button>
        ))}
      </div>
      {selection.screen === "play" && points.length > 0 && (
        <label className="mb-3 flex items-center gap-2 text-xs">
          播放页预览内容
          <select
            aria-label="播放页预览内容"
            value={choiceId}
            className="max-w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-3 py-2"
            onChange={(event) => setChoiceId(event.target.value)}
          >
            <option value="">视频与功能按钮</option>
            {points.map((item) => (
              <option
                key={item.element.element_id}
                value={item.element.element_id}
              >
                {project.timelines.items[item.tid].title} ·{" "}
                {item.element.creation.type === "interaction"
                  ? item.element.creation.question
                  : ""}
              </option>
            ))}
          </select>
        </label>
      )}
      {point ? (
        <ChoiceEditor
          key={point.element.element_id}
          project={project}
          timelineId={point.tid}
          element={point.element}
          status={statuses[`interaction:${point.element.element_id}`] ?? ""}
          renderPreview={(onSelect) => (
            <PresentationPreview
              project={project}
              selection={{ screen: "play" }}
              reviewPointId={point.element.element_id}
              onChoiceInspect={onSelect}
            />
          )}
        />
      ) : (
        <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
          <PresentationPreview
            project={project}
            selection={selection}
            onInspect={setSelection}
            onControls={setControls}
          />
          <aside
            className="min-w-0 space-y-4 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-primary)] p-4"
            role="tabpanel"
            aria-label={`${page.label}设计`}
            data-interaction-details
          >
            <div>
              <strong className="text-sm">{page.label}</strong>
              <span className="ml-2 text-xs">
                {generationLabels[status] ?? status}
              </span>
              <p className="mt-1 text-xs text-[var(--color-text-secondary)]">
                {page.purpose}
              </p>
            </div>
            <div>
              <p className="mb-2 text-xs font-medium">
                功能按钮 · 点击查看详情
              </p>
              <div className="flex flex-wrap gap-2">
                {buttonIds.map((id) => (
                  <button
                    type="button"
                    key={id}
                    className="btn-secondary"
                    onClick={() =>
                      setSelection({ screen: page.id, action: id })
                    }
                    aria-pressed={selection.action === id}
                  >
                    {actions[id]?.label ?? id}
                    {page.required.includes(id) ? " · 必备" : ""}
                  </button>
                ))}
              </div>
            </div>
            {selection.action && (
              <div
                className="space-y-3"
                data-control-editor={`${page.id}.${selection.action}`}
              >
                <p className="text-xs text-[var(--color-text-secondary)]">
                  {actions[selection.action].behavior}
                </p>
                {!actual && (
                  <p className="text-xs">
                    当前生成结果尚未包含此按钮，重新生成时会补齐并校验。
                  </p>
                )}
                <GenerationPromptEditor
                  target={{
                    pointer: `${pagePath}/controls/${selection.action}/label`,
                    label: "按钮文案",
                    value: chosen?.label || actual?.label || "",
                  }}
                  saving={generation.locked}
                  regenerateLabel=""
                  onSave={(_, next) => updateControl({ label: next })}
                />
              </div>
            )}
            <GenerationPromptEditor
              key={promptPath}
              target={{
                pointer: promptPath,
                label: selection.action
                  ? "按钮外观与动效提示词"
                  : `${page.label}生成提示词`,
                value:
                  (selection.action
                    ? chosen?.design_prompt
                    : pageDesign?.design_prompt) ?? "",
              }}
              saving={generation.locked}
              regenerateLabel={
                generation.busy || status === "running"
                  ? "生成中…"
                  : design?.motion
                  ? "重新生成页面"
                  : "生成作品页面"
              }
              onSave={(_, next) =>
                selection.action
                  ? updateControl({ design_prompt: next })
                  : updateScreen({ design_prompt: next })
              }
              onRegenerate={() => generation.generate("interaction:project")}
            />
            <details className="text-xs">
              <summary className="cursor-pointer">整体视觉与动效提示词</summary>
              <div className="mt-3">
                <GenerationPromptEditor
                  target={{
                    pointer: "/interactive_presentation/design_prompt",
                    label: "整体生成提示词",
                    value: design?.design_prompt ?? "",
                  }}
                  saving={generation.locked}
                  regenerateLabel=""
                  onSave={(_, next) =>
                    generation.save([
                      {
                        op: "replace",
                        path: "/interactive_presentation/design_prompt",
                        before: design?.design_prompt ?? "",
                        value: next,
                      },
                    ])
                  }
                />
              </div>
            </details>
            {page.optional.some((id) => !buttonIds.includes(id)) && (
              <label className="block text-xs">
                按剧情需要增加功能
                <select
                  aria-label="增加可选功能"
                  disabled={generation.locked}
                  value=""
                  className="mt-1 w-full rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-primary)] p-2"
                  onChange={(event) => {
                    const action = event.target.value as PresentationActionId;
                    if (!action) return;
                    void updateScreen({
                      controls: {
                        ...pageDesign?.controls,
                        [action]: { label: "", design_prompt: "" },
                      },
                    })
                      .then(() => setSelection({ screen: page.id, action }))
                      .catch(() => {});
                  }}
                >
                  <option value="">选择功能</option>
                  {page.optional
                    .filter((id) => !buttonIds.includes(id))
                    .map((id) => (
                      <option key={id} value={id}>
                        {actions[id].label}
                      </option>
                    ))}
                </select>
              </label>
            )}
            <p className="text-xs text-[var(--color-text-secondary)]">
              编辑完成后保存提示词；重新生成更新作品页面，完成后进入审阅。
            </p>
            {status === "waiting_review" && (
              <p className="text-xs">请先在审阅面板批准或拒绝本次生成。</p>
            )}
          </aside>
        </div>
      )}
    </article>
  );
}
