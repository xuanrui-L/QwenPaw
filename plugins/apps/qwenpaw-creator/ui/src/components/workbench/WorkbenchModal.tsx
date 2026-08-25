import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Modal } from "antd";
import { Maximize2, Minimize2, X } from "lucide-react";
import { useAgentDockUiStore } from "@/store/agentDockUiStore";
import { WorkbenchSurface } from "@/pages/R2VWorkbenchPage";
import type { TimelineElementDocument } from "@/contracts/creator";

// 制作台悬浮窗：在方案页原地以居中毛玻璃窗承载 WorkbenchSurface。
// 遮罩与窗体都只覆盖工作区（右缘停在 AgentDock 左侧），Dock 始终可见可输入；
// Dock 宽度可拖拽调整时，这里通过 store 宽度实时避让。
const MODAL_WIDTH: Record<string, number> = {
  r2v: 1240,
  t2v: 620,
  i2v: 680,
  s2v: 680,
};

export default function WorkbenchModal({
  projectId,
  element,
  onClose,
}: {
  projectId: string;
  element: TimelineElementDocument;
  onClose: () => void;
}) {
  const dockWidth = useAgentDockUiStore((state) => state.width);
  const [maximized, setMaximized] = useState(false);
  const dirtyRef = useRef(false);
  const creationType = element.creation.type;
  const width = MODAL_WIDTH[creationType] ?? 640;

  // 关闭守护：制作台内有未应用修改时，先确认再关（与路由页 backToPlan 一致）。
  const requestClose = useCallback(() => {
    if (!dirtyRef.current) {
      onClose();
      return;
    }
    Modal.confirm({
      title: "还有未应用的修改",
      content: "关闭制作台会放弃当前草稿。",
      okText: "放弃并关闭",
      okButtonProps: { danger: true },
      cancelText: "继续编辑",
      onOk: onClose,
    });
  }, [onClose]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") requestClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [requestClose]);

  const controls = (
    <>
      {creationType === "r2v" && (
        <button
          type="button"
          onClick={() => setMaximized((value) => !value)}
          className="icon-button shrink-0"
          aria-label={maximized ? "退出全屏" : "全屏"}
          title={maximized ? "退出全屏" : "全屏"}
        >
          {maximized ? (
            <Minimize2 className="h-3.5 w-3.5" />
          ) : (
            <Maximize2 className="h-3.5 w-3.5" />
          )}
        </button>
      )}
      <button
        type="button"
        onClick={requestClose}
        className="icon-button shrink-0"
        aria-label="关闭制作台"
        title="关闭制作台 (Esc)"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </>
  );

  return createPortal(
    <>
      {/* 遮罩只覆盖工作区：right 让位给 AgentDock（宽度实时来自 store） */}
      <div
        data-workbench-modal-scrim
        className="fixed bottom-0 left-0 top-0 z-40 bg-[rgba(20,16,12,0.38)] backdrop-blur-[5px]"
        style={{ right: dockWidth }}
        onClick={requestClose}
      />
      <div
        className="pointer-events-none fixed bottom-0 left-0 top-0 z-41 grid place-items-center p-7"
        style={{ right: dockWidth, zIndex: 41 }}
      >
        <div
          data-workbench-modal={element.element_id}
          className="pointer-events-auto flex max-h-[88vh] flex-col overflow-hidden rounded-[20px] border backdrop-blur-[28px] backdrop-saturate-[1.15]"
          style={{
            width: maximized ? "100%" : `min(${width}px, 100%)`,
            height:
              creationType === "r2v"
                ? maximized
                  ? "94vh"
                  : "min(820px, 88vh)"
                : undefined,
            maxHeight: maximized ? "94vh" : "88vh",
            borderColor:
              "color-mix(in srgb, #ffffff 55%, var(--color-border))",
            background:
              "color-mix(in srgb, var(--color-bg-layout) 82%, transparent)",
            boxShadow:
              "0 32px 80px rgba(20,16,12,.35), inset 0 1px 0 rgba(255,255,255,.55)",
          }}
        >
          <WorkbenchSurface
            projectId={projectId}
            elementId={element.element_id}
            embedded
            onBack={onClose}
            onDirtyChange={(dirty) => {
              dirtyRef.current = dirty;
            }}
            headerExtra={controls}
          />
        </div>
      </div>
    </>,
    document.body,
  );
}
