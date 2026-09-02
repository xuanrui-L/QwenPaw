import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, Trash2 } from "lucide-react";
import type {
  CreatorScenario,
  VideoTemplateSummary,
} from "@/contracts/creator";
import { deleteVideoTemplate, listVideoTemplates } from "@/api/creator";

interface VideoTemplatePickerProps {
  selectedTemplateId: string | null;
  onTemplateSelect: (templateId: string | null) => void;
  scenario: CreatorScenario;
}

export default function VideoTemplatePicker({
  selectedTemplateId,
  onTemplateSelect,
  scenario,
}: VideoTemplatePickerProps) {
  const { t } = useTranslation();
  const [templates, setTemplates] = useState<VideoTemplateSummary[]>([]);

  const reload = useCallback(() => {
    listVideoTemplates()
      .then((res) => setTemplates(res.items ?? []))
      .catch(() => setTemplates([]));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  if (templates.length === 0) return null;

  const builtins = templates.filter((t) => t.source === "builtin");
  const userTemplates = templates.filter((t) => t.source === "user");

  async function handleDelete(e: React.MouseEvent, tpl: VideoTemplateSummary) {
    e.stopPropagation();
    if (!window.confirm(t("home.deleteTemplateConfirm", { name: tpl.name })))
      return;
    try {
      await deleteVideoTemplate(tpl.templateId);
      if (selectedTemplateId === tpl.templateId) onTemplateSelect(null);
      reload();
    } catch {
      /* toast handled by API layer */
    }
  }

  function renderCard(tpl: VideoTemplateSummary) {
    const selected = selectedTemplateId === tpl.templateId;
    return (
      <div key={tpl.templateId} className="relative shrink-0">
        <button
          type="button"
          onClick={() => onTemplateSelect(tpl.templateId)}
          className={`flex h-[72px] w-[120px] flex-col items-center justify-center gap-0.5 rounded-lg border px-2 transition-colors ${
            selected
              ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]"
              : "border-[var(--color-border)] bg-white hover:border-[var(--color-text-tertiary)]"
          }`}
        >
          <span className="text-lg leading-none">{tpl.iconEmoji}</span>
          <span className="text-[11px] font-medium leading-tight text-[var(--color-text-primary)]">
            {tpl.name}
          </span>
          <span className="line-clamp-1 text-[9px] leading-tight text-[var(--color-text-tertiary)]">
            {tpl.previewDescription}
          </span>
          {selected && (
            <Check className="absolute right-1 top-1 h-3 w-3 text-[var(--color-accent)]" />
          )}
        </button>
        {tpl.source === "user" && (
          <button
            type="button"
            onClick={(e) => handleDelete(e, tpl)}
            className="absolute -right-1 -top-1 hidden rounded-full bg-red-500 p-0.5 text-white group-hover:flex"
            aria-label="Delete"
          >
            <Trash2 className="h-3 w-3" />
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="px-4 pb-2">
      <div className="mb-1.5 text-[11px] font-medium text-[var(--color-text-tertiary)]">
        {t("home.videoTemplateHint")}
      </div>
      <div className="flex flex-wrap gap-2">
        <div className="group relative shrink-0">
          <button
            type="button"
            onClick={() => onTemplateSelect(null)}
            className={`flex h-[72px] w-[120px] flex-col items-center justify-center gap-1 rounded-lg border transition-colors ${
              selectedTemplateId === null
                ? "border-[var(--color-accent)] bg-[var(--color-accent-soft)]"
                : "border-[var(--color-border)] bg-white hover:border-[var(--color-text-tertiary)]"
            }`}
          >
            <span className="text-lg">{"\u2728"}</span>
            <span className="text-[11px] font-medium text-[var(--color-text-secondary)]">
              {t("home.videoTemplateNone")}
            </span>
            {selectedTemplateId === null && (
              <Check className="h-3 w-3 text-[var(--color-accent)]" />
            )}
          </button>
        </div>
        {builtins.map(renderCard)}
      </div>
      {userTemplates.length > 0 && (
        <>
          <div className="mb-1 mt-2 text-[11px] font-medium text-[var(--color-text-tertiary)]">
            {t("home.videoTemplateUserSection")}
          </div>
          <div className="flex flex-wrap gap-2">
            {userTemplates.map((tpl) => (
              <div key={tpl.templateId} className="group relative shrink-0">
                {renderCard(tpl)}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
