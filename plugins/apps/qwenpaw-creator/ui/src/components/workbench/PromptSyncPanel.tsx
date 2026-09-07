import { useEffect, useRef, useState } from "react";
import { FileCheck2, RefreshCw, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  getPromptSync,
  type PromptSyncScope,
  type PromptSyncState,
} from "@/api/creator/promptSync";

interface Props extends PromptSyncScope {
  generation: number | null;
  dirty: boolean;
  working?: boolean;
  onStatus: (state: PromptSyncState | null) => void;
}

/** Read-only context; regeneration owns synchronization as one user action. */
export default function PromptSyncPanel(props: Props) {
  const { t } = useTranslation();
  const [state, setState] = useState<PromptSyncState | null>(null);
  const [loadedKey, setLoadedKey] = useState("");
  const [failed, setFailed] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const latest = useRef(props);
  latest.current = props;
  const scope = {
    projectId: props.projectId,
    timelineId: props.timelineId,
    elementId: props.elementId,
  };
  const loadKey = JSON.stringify([scope, props.generation]);
  useEffect(() => {
    const controller = new AbortController();
    setState(null);
    setFailed(false);
    latest.current.onStatus(null);
    getPromptSync(scope, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setState(next);
        setLoadedKey(loadKey);
        latest.current.onStatus(next);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => controller.abort();
  }, [loadKey]);
  useEffect(() => {
    if (!props.working) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const timer = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [props.working]);
  const status = props.dirty
    ? "draft"
    : loadedKey === loadKey
    ? state?.status ?? "checking"
    : "checking";
  const needsSync =
    status === "needs_update" || status === "needs_confirmation";
  const validationMessage =
    !props.dirty && loadedKey === loadKey ? state?.validationMessage : null;
  return (
    <div
      className="r2v-prompt-sync"
      data-prompt-sync-status={status}
      data-prompt-sync-error={Boolean(validationMessage) && !props.working}
    >
      <div className="r2v-prompt-sync-summary" role="status">
        <span
          className={`r2v-sync-mark ${props.working ? "is-working" : ""}`}
          aria-hidden
        >
          {props.working ? (
            <RefreshCw size={14} />
          ) : validationMessage ? (
            <TriangleAlert size={14} />
          ) : (
            <FileCheck2 size={14} />
          )}
        </span>
        <p className="r2v-sync-description">
          {props.working
            ? t("r2v.sync.automaticWaiting", { seconds: elapsed })
            : validationMessage
            ? validationMessage
            : failed
            ? t("r2v.sync.checkOnGenerate")
            : needsSync
            ? t("r2v.sync.savedNextGenerate")
            : t(`r2v.sync.status.${status}`)}
        </p>
      </div>
    </div>
  );
}
