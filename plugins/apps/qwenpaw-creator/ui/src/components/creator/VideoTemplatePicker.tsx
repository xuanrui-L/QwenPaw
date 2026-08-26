import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Check } from "lucide-react";
import type {
  CreatorScenario,
  VideoTemplateSummary,
} from "@/contracts/creator";
import { listVideoTemplates } from "@/api/creator";

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

  useEffect(() => {
    listVideoTemplates()
      .then((res) => setTemplates(res.items))
      .catch(() => setTemplates([]));
  }, []);

  if (templates.length === 0) return null;

  return (
    <div className="px-4 pb-2">
      <div className="mb-1.5 text-[11px] font-medium text-[var(--color-text-tertiary)]">
        {t("home.videoTemplateHint")}
      </div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        <button
          type="button"
          onClick={() => onTemplateSelect(null)}
          className={`flex h-[72px] w-[120px] shrink-0 flex-col items-center justify-center gap-1 rounded-lg border transition-colors ${
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
        {templates.map((tpl) => (
          <button
            key={tpl.templateId}
            type="button"
            onClick={() => onTemplateSelect(tpl.templateId)}
            className={`flex h-[72px] w-[120px] shrink-0 flex-col items-center justify-center gap-0.5 rounded-lg border px-2 transition-colors ${
              selectedTemplateId === tpl.templateId
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
            {selectedTemplateId === tpl.templateId && (
              <Check className="absolute right-1 top-1 h-3 w-3 text-[var(--color-accent)]" />
            )}
          </button>
        ))}
      </div>
    </div>
  );
}
