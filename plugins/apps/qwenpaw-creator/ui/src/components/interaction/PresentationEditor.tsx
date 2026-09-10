import { useState } from "react";
import { message } from "antd";
import type {
  InteractivePresentationDocument,
  InteractiveScreenDesignDocument,
  PresentationActionId,
  PresentationScreenId,
  ProjectDocument,
} from "@/contracts/creator";
import { dispatchWorkGraphNode } from "@/api/creator/workGraph";
import { useProjectSnapshotStore } from "@/store/projectSnapshotStore";
import { actions, screens, type PreviewControl } from "./presentationDesign";
import { PresentationPreview } from "./PresentationPreview";
import { useDesignDraft } from "./useDesignDraft";

export { PresentationPreview } from "./PresentationPreview";
type Design = Pick<
  InteractivePresentationDocument,
  "design_prompt" | "screens"
>;
export const generationLabels: Record<string, string> = {
  ready: "待生成",
  running: "生成中",
  done: "已生成",
  waiting_review: "待审阅",
  failed: "生成失败",
  gated: "等待前置内容",
};
const fieldClass =
  "mt-1 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-secondary)] p-2 text-sm";

export default function PresentationEditor({
  project,
  status = "",
}: {
  project: ProjectDocument;
  status?: string;
}) {
  const initial = JSON.stringify({
    design_prompt: project.interactive_presentation?.design_prompt ?? "",
    screens: project.interactive_presentation?.screens ?? {},
  });
  const draft = useDesignDraft(initial);
  const design = JSON.parse(draft.value) as Design;
  const [busy, setBusy] = useState(false);
  const [selection, setSelection] = useState<{
    screen: PresentationScreenId;
    action?: PresentationActionId;
  }>({ screen: "title" });
  const [controls, setControls] = useState<PreviewControl[]>([]);
  const patch = useProjectSnapshotStore((s) => s.patch);
  const page = screens.find((item) => item.id === selection.screen)!;
  const pageDesign = design.screens?.[page.id];
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
    draft.change(
      JSON.stringify({
        ...design,
        screens: {
          ...design.screens,
          [page.id]: {
            design_prompt: "",
            controls: {},
            ...pageDesign,
            ...value,
          },
        },
      }),
    );
  const updateControl = (value: { label?: string; design_prompt?: string }) => {
    if (!selection.action) return;
    updateScreen({
      controls: {
        ...pageDesign?.controls,
        [selection.action]: {
          label: "",
          design_prompt: "",
          ...pageDesign?.controls?.[selection.action],
          ...value,
        },
      },
    });
  };
  const locked =
    busy ||
    status === "running" ||
    status === "waiting_review" ||
    draft.conflict;
  const save = async (generate: boolean) => {
    setBusy(true);
    try {
      if (draft.dirty) {
        const base = JSON.parse(draft.base) as Design;
        await patch(project.project_id, [
          {
            op: "replace",
            path: "/interactive_presentation/design_prompt",
            before: base.design_prompt,
            value: design.design_prompt,
          },
          {
            op: project.interactive_presentation?.screens ? "replace" : "add",
            path: "/interactive_presentation/screens",
            before: base.screens,
            missingBefore: !project.interactive_presentation?.screens,
            value: design.screens,
          },
        ]);
        draft.saved();
      }
      if (generate) {
        const result = await dispatchWorkGraphNode(
          project.project_id,
          "interaction:project",
        );
        message.info(
          result.status === "done"
            ? "当前页面已生成；修改设计后可重新生成"
            : "页面生成已提交，完成后请预览并审阅",
        );
      } else message.success("设计已保存，生成后可查看新的效果");
    } catch (err) {
      message.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const chosen = selection.action
    ? pageDesign?.controls?.[selection.action]
    : undefined;
  const actual = controls.find(
    (control) =>
      control.screen === page.id && control.action === selection.action,
  );
  return (
    <article
      data-creator-path="/interactive_presentation/motion"
      data-presentation-editor
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <strong>作品界面</strong>
          <span className="ml-3 text-xs">
            {generationLabels[status] ?? status}
          </span>
        </div>
        <span className="text-xs text-[var(--color-text-secondary)]">
          四个页面 · 开始、重新开始、剧情地图为必备功能
        </span>
      </div>
      <label className="block text-xs">
        整体视觉与动效要求
        <textarea
          aria-label="整体视觉与动效要求"
          className={fieldClass}
          rows={2}
          value={design.design_prompt}
          onChange={(e) =>
            draft.change(
              JSON.stringify({ ...design, design_prompt: e.target.value }),
            )
          }
        />
      </label>
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
            className={page.id === item.id ? "btn-primary" : "btn-secondary"}
            key={item.id}
            onClick={() => setSelection({ screen: item.id })}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_280px]">
        <PresentationPreview
          project={project}
          selection={selection}
          onInspect={setSelection}
          onControls={setControls}
        />
        <div
          className="min-w-0 rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-primary)] p-3"
          role="tabpanel"
          aria-label={`${page.label}设计`}
        >
          <strong className="text-sm">{page.label}</strong>
          <p className="my-1 text-xs text-[var(--color-text-secondary)]">
            {page.purpose}
          </p>
          <label className="my-3 block text-xs">
            {page.label}设计要求
            <textarea
              aria-label={`${page.label}设计要求`}
              className={fieldClass}
              rows={3}
              value={pageDesign?.design_prompt ?? ""}
              onChange={(e) => updateScreen({ design_prompt: e.target.value })}
            />
          </label>
          <p className="mb-2 text-xs font-medium">
            功能按钮 · 点击可在预览中定位
          </p>
          <div className="flex flex-wrap gap-1">
            {buttonIds.map((id) => (
              <button
                type="button"
                key={id}
                className={
                  selection.action === id ? "btn-primary" : "btn-secondary"
                }
                onClick={() => setSelection({ screen: page.id, action: id })}
                aria-pressed={selection.action === id}
              >
                {actions[id]?.label ?? id}
                {page.required.includes(id) ? " · 必备" : ""}
              </button>
            ))}
          </div>
          {selection.action && (
            <div
              className="mt-3 border-t border-[var(--color-border)] pt-3"
              data-control-editor={`${page.id}.${selection.action}`}
            >
              <p className="text-xs">{actions[selection.action].behavior}</p>
              <p className="my-2 text-xs text-[var(--color-text-secondary)]">
                {actual
                  ? `生成文案：${actual.label}`
                  : "当前页面还没有此按钮，生成时会检查是否实现。"}
              </p>
              <label className="block text-xs">
                按钮文案
                <input
                  aria-label="按钮文案"
                  className={fieldClass}
                  value={chosen?.label ?? ""}
                  placeholder="留空由助手按剧情创作"
                  onChange={(e) => updateControl({ label: e.target.value })}
                />
              </label>
              <label className="mt-2 block text-xs">
                按钮外观与动效
                <textarea
                  aria-label="按钮外观与动效"
                  className={fieldClass}
                  rows={3}
                  value={chosen?.design_prompt ?? ""}
                  onChange={(e) =>
                    updateControl({ design_prompt: e.target.value })
                  }
                />
              </label>
            </div>
          )}
          {page.optional.filter((id) => !buttonIds.includes(id)).length > 0 && (
            <label className="mt-3 block text-xs">
              按剧情需要增加功能
              <select
                aria-label="增加可选功能"
                className={fieldClass}
                value=""
                onChange={(e) => {
                  const action = e.target.value as PresentationActionId;
                  if (!action) return;
                  updateScreen({
                    controls: {
                      ...pageDesign?.controls,
                      [action]: { label: "", design_prompt: "" },
                    },
                  });
                  setSelection({ screen: page.id, action });
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
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-secondary"
          disabled={locked || !draft.dirty}
          onClick={() => void save(false)}
        >
          保存设计
        </button>
        <button
          type="button"
          className="btn-primary"
          disabled={locked}
          onClick={() => void save(true)}
        >
          {busy
            ? "提交中…"
            : draft.dirty
            ? "保存并生成页面"
            : project.interactive_presentation?.motion
            ? "重新生成页面"
            : "生成作品页面"}
        </button>
        {draft.dirty && (
          <button type="button" className="btn-secondary" onClick={draft.reset}>
            撤销未保存修改
          </button>
        )}
        <span className="text-xs">
          {draft.dirty
            ? "有未保存的设计修改；预览仍为上次生成结果。"
            : "设计修改将在重新生成后呈现，不会重做视频。"}
        </span>
      </div>
      {draft.conflict && (
        <p role="alert" className="mt-2 text-sm">
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
