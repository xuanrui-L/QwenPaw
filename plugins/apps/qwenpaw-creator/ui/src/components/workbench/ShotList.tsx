import { Select, Button, InputNumber, Input, Popconfirm } from "antd";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { useTranslation } from "react-i18next";
import type {
  ProjectEntityCollection,
  ShotDocument,
} from "@/contracts/creator";
import { useCreatorInteractionStore } from "@/store/creatorInteractionStore";
import InlineReviewDiff from "@/components/agent/InlineReviewDiff";
import { shotContentText } from "@/lib/shotContent";

const { TextArea } = Input;

// Values match backend ShotCamera / ShotFraming. Custom direction belongs
// in the complete description rather than an unsupported enum value.
const CAMERA_PRESETS = [
  "⊙ 静止",
  "↑ 推近",
  "↓ 拉远",
  "→ 横摇右",
  "← 横摇左",
  "↕ 升降",
  "◎ 环绕",
  "～ 手持晃动",
];
const FRAMING_PRESETS = ["全景", "中景", "近景", "特写"];
const CAMERA_OPTIONS = CAMERA_PRESETS.map((value) => ({ value }));
const FRAMING_OPTIONS = FRAMING_PRESETS.map((value) => ({ value }));

type ShotField =
  | "description"
  | "camera"
  | "framing"
  | "duration_seconds"
  | "dialogue";

interface ShotListProps {
  shots: ProjectEntityCollection<ShotDocument>;
  elementId: string;
  shotPointer: (shotId: string, field: ShotField) => string;
  disabled?: boolean;
  onChangeField: (shotId: string, field: ShotField, value: unknown) => void;
  onAdd: () => void;
  onDelete: (shot: ShotDocument) => void;
}

/** A single content plan; the parent separately prepares both media instructions. */
export default function ShotList({
  shots,
  elementId,
  shotPointer,
  disabled,
  onChangeField,
  onAdd,
  onDelete,
}: ShotListProps) {
  const { t } = useTranslation();
  const trackShotFocus = (shotId: string, field: ShotField) => {
    const store = useCreatorInteractionStore.getState();
    store.select(`element:${elementId}`);
    store.setEditingField(`element:${elementId}/shot:${shotId}/${field}`);
  };

  const releaseShotFocus = () => {
    useCreatorInteractionStore.getState().setEditingField(null);
  };

  return (
    <div className="space-y-2">
      {shots.order.map((shotId, index) => {
        const shot = shots.items[shotId];
        if (!shot) return null;
        return (
          <div
            key={shotId}
            data-creator-module="shot-row"
            data-creator-module-id={shotId}
            data-creator-module-ref={`element:${elementId}`}
            data-shot-index={index}
            className="r2v-plan-shot"
          >
            <div className="r2v-plan-shot-heading">
              <span>{t("r2v.plan.shot", { index: index + 1 })}</span>
              <span title={t("r2v.plan.actionLabel")}>
                {t("r2v.plan.actionLabel")}
              </span>
              <Popconfirm
                title={t("workbench.deleteShot")}
                onConfirm={() => onDelete(shot)}
                okText={t("workbench.delete")}
                cancelText={t("workbench.cancel")}
              >
                <Button
                  type="text"
                  size="small"
                  disabled={disabled || shots.order.length <= 1}
                  aria-label={t("r2v.plan.remove", { index: index + 1 })}
                  icon={<DeleteOutlined />}
                  className="!ml-auto !h-6 !w-6 !min-w-6 !shrink-0 !p-0 !text-[var(--color-text-tertiary)] hover:!text-[var(--color-danger)]"
                />
              </Popconfirm>
            </div>
            <div
              className="min-w-0"
              data-creator-field={`element:${elementId}/shot:${shotId}/description`}
              data-creator-path={shotPointer(shotId, "description")}
              data-review-field={shotPointer(shotId, "dialogue")}
              data-creator-field-label={t("lib.shotDescription", {
                index: index + 1,
              })}
            >
              <TextArea
                value={shotContentText(shot, t("r2v.plan.dialogueLabel"))}
                aria-label={t("r2v.plan.actionAria", { index: index + 1 })}
                disabled={disabled}
                onChange={(event) =>
                  onChangeField(shotId, "description", event.target.value)
                }
                onFocus={() => trackShotFocus(shotId, "description")}
                onBlur={releaseShotFocus}
                autoSize={{ minRows: 3, maxRows: 10 }}
                placeholder={t("r2v.plan.contentHint")}
                className="!rounded-md !border-transparent !bg-transparent !p-1 !text-[13px] !leading-6 hover:!border-[var(--color-border)] focus:!border-[var(--color-accent)]"
              />
              <InlineReviewDiff pointer={shotPointer(shotId, "description")} />
              <InlineReviewDiff pointer={shotPointer(shotId, "dialogue")} />
            </div>
            <div className="r2v-plan-shot-controls grid gap-1.5">
              <label
                data-creator-field={`element:${elementId}/shot:${shotId}/camera`}
                data-creator-path={shotPointer(shotId, "camera")}
                data-creator-field-label={t("lib.shotCamera", {
                  index: index + 1,
                })}
                className="min-w-0"
              >
                <span className="mb-0.5 block text-[10px] leading-3 text-[var(--color-text-tertiary)]">
                  {t("workbench.shotCameraLabel")}
                </span>
                <Select
                  value={shot.camera ?? ""}
                  disabled={disabled}
                  options={CAMERA_OPTIONS}
                  onChange={(value) =>
                    onChangeField(shotId, "camera", value ?? "")
                  }
                  onFocus={() => trackShotFocus(shotId, "camera")}
                  onBlur={releaseShotFocus}
                  size="small"
                  placeholder={t("workbench.shotCamera")}
                  className="!w-full !text-[11px]"
                />
              </label>
              <label
                data-creator-field={`element:${elementId}/shot:${shotId}/framing`}
                data-creator-path={shotPointer(shotId, "framing")}
                data-creator-field-label={t("lib.shotFraming", {
                  index: index + 1,
                })}
                className="min-w-0"
              >
                <span className="mb-0.5 block text-[10px] leading-3 text-[var(--color-text-tertiary)]">
                  {t("workbench.shotFraming")}
                </span>
                <Select
                  value={shot.framing ?? ""}
                  disabled={disabled}
                  options={FRAMING_OPTIONS}
                  onChange={(value) =>
                    onChangeField(shotId, "framing", value ?? "")
                  }
                  onFocus={() => trackShotFocus(shotId, "framing")}
                  onBlur={releaseShotFocus}
                  size="small"
                  placeholder={t("workbench.shotFraming")}
                  className="!w-full !text-[11px]"
                />
              </label>
              <label
                data-creator-field={`element:${elementId}/shot:${shotId}/duration_seconds`}
                data-creator-path={shotPointer(shotId, "duration_seconds")}
                data-creator-field-label={t("lib.shotDuration", {
                  index: index + 1,
                })}
                className="min-w-0"
              >
                <span className="mb-0.5 block text-[10px] leading-3 text-[var(--color-text-tertiary)]">
                  {t("workbench.shotDurationLabel")}
                </span>
                <InputNumber
                  min={1}
                  value={shot.duration_seconds}
                  disabled={disabled}
                  onChange={(value) =>
                    onChangeField(shotId, "duration_seconds", value ?? 1)
                  }
                  onFocus={() => trackShotFocus(shotId, "duration_seconds")}
                  onBlur={releaseShotFocus}
                  size="small"
                  suffix="s"
                  controls={false}
                  className="!w-full"
                />
              </label>
            </div>
            <InlineReviewDiff pointer={shotPointer(shotId, "camera")} />
            <InlineReviewDiff pointer={shotPointer(shotId, "framing")} />
            <InlineReviewDiff
              pointer={shotPointer(shotId, "duration_seconds")}
            />
          </div>
        );
      })}
      <Button
        type="dashed"
        size="small"
        icon={<PlusOutlined />}
        onClick={onAdd}
        disabled={disabled}
        className="!w-full !text-xs"
      >
        {t("workbench.addShot")}
      </Button>
    </div>
  );
}
