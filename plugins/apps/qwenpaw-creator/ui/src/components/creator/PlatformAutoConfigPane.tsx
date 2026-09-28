import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Alert, Button } from "antd";
import {
  applyPlatformCredentials,
  fetchPlatformCredentials,
  type PlatformApplyResult,
} from "@/api/creator/platform";

/**
 * Platform "configure in one click" pane.
 *
 * Exists for one deployment only: Creator as a container on a QwenPaw subdomain,
 * where the platform owns the model key and will release it to the browser (via
 * the subdomain's console_token cookie) but never to the container process.
 * That asymmetry forces the shape below - the browser reads the credential and
 * hands it to Creator's backend to persist.
 *
 * The pane is intentionally always visible rather than feature-detected: a
 * failure here is the platform deployment being unreachable, which the operator
 * should see instead of a missing button.
 */

const SECTION_LABEL_KEYS: Record<string, string> = {
  llm: "modelConfig.llm",
  vlm: "modelConfig.vlm",
  image: "modelConfig.imageGen",
  video: "modelConfig.videoGen",
  tts: "modelConfig.tts",
  asr: "modelConfig.asr",
};

function formatCredits(value: unknown): string | null {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "string" && value.trim()) return value.trim();
  return null;
}

export default function PlatformAutoConfigPane({
  onJumpToModel,
}: {
  onJumpToModel: (section: string) => void;
}) {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [credits, setCredits] = useState<string | null>(null);
  const [result, setResult] = useState<PlatformApplyResult | null>(null);

  const run = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const credentials = await fetchPlatformCredentials();
      setCredits(formatCredits(credentials.display_available_credits));
      setResult(await applyPlatformCredentials(credentials));
    } catch (exc) {
      setResult(null);
      setError((exc as Error).message || t("modelConfig.platform.applyFailed"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  return (
    <>
      <div>
        <div
          style={{
            fontSize: 12,
            color: "var(--color-text-tertiary)",
            lineHeight: 1.6,
          }}
        >
          {t("modelConfig.panePlatformDesc")}
        </div>
      </div>

      <div
        className="glass-card"
        style={{
          padding: "16px 18px",
          borderRadius: 8,
          boxShadow: "var(--shadow-xs)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <Button type="primary" loading={loading} onClick={() => void run()}>
            {t("modelConfig.platform.action")}
          </Button>
          {credits !== null && (
            <span
              style={{ fontSize: 12, color: "var(--color-text-secondary)" }}
            >
              {t("modelConfig.platform.credits", { value: credits })}
            </span>
          )}
        </div>
        <div
          style={{
            fontSize: 12,
            color: "var(--color-text-tertiary)",
            marginTop: 10,
            lineHeight: 1.6,
          }}
        >
          {t("modelConfig.platform.coverage")}
        </div>
      </div>

      {error && (
        <Alert
          type="error"
          showIcon
          message={error}
          style={{ borderRadius: 8 }}
        />
      )}

      {result && (
        <div
          className="glass-card"
          style={{
            padding: "14px 18px",
            borderRadius: 8,
            boxShadow: "var(--shadow-xs)",
            display: "flex",
            flexDirection: "column",
            gap: 8,
          }}
        >
          <div style={{ fontSize: 12.5, color: "var(--color-text-secondary)" }}>
            {t("modelConfig.platform.endpoint", { url: result.base_url })}
          </div>
          {result.sections.map((section) => (
            <div
              key={section.section}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 12,
                fontSize: 12.5,
              }}
            >
              <span style={{ color: "var(--color-text-primary)" }}>
                {SECTION_LABEL_KEYS[section.section]
                  ? t(SECTION_LABEL_KEYS[section.section])
                  : section.section}
              </span>
              {section.ready ? (
                <span style={{ color: "var(--color-success)" }}>
                  {section.model_name}
                </span>
              ) : (
                <Button
                  type="link"
                  size="small"
                  style={{ padding: 0, height: "auto", fontSize: 12.5 }}
                  onClick={() => onJumpToModel(section.section)}
                >
                  {t("modelConfig.platform.needsModel")}
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
