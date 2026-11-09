import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button, Input, Modal, Spin, message } from "antd";
import {
  Download,
  Globe,
  Search,
  User,
  AppWindow,
  ShoppingBag,
} from "lucide-react";
import {
  importSkillFromUrl,
  searchMarketSkills,
  type MarketSkillResult,
} from "@/api/creator";

/** Market source label lookup for display purposes. */
const KNOWN_SOURCES: Record<string, string> = {
  qwenpaw: "QwenPaw",
  "skills-sh": "Skills.sh",
  github: "GitHub",
  clawhub: "ClawHub",
  lobehub: "LobeHub",
  modelscope: "ModelScope",
  aliyun: "Aliyun",
  skillsmp: "SkillsMP",
};

function sourceLabel(source: string, label: string | null): string {
  return label || KNOWN_SOURCES[source] || source;
}

interface SkillMarketBrowserProps {
  open: boolean;
  onClose: () => void;
  /** Called after a successful install so the pane can refresh its list. */
  onInstalled: () => void;
  /** Currently installed skill names, for disabling already-installed items. */
  installedNames: string[];
}

export default function SkillMarketBrowser({
  open,
  onClose,
  onInstalled,
  installedNames,
}: SkillMarketBrowserProps) {
  const { t, i18n } = useTranslation();
  const lang = i18n.language || "en";
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<MarketSkillResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [page, setPage] = useState(1);
  const [installing, setInstalling] = useState<Set<string>>(new Set());
  const [installed, setInstalled] = useState<Set<string>>(new Set());
  const searchSeqRef = useRef(0);
  const resultsRef = useRef<MarketSkillResult[]>([]);
  const inputRef = useRef<Input>(null);
  // Synchronous guard so rapid double-clicks cannot pass the installing
  // check before React commits the first setInstalling update.
  const installingRef = useRef<Set<string>>(new Set());

  // Focus search input when modal opens.
  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 100);
    }
  }, [open]);

  // Sync installedNames into installed set.
  useEffect(() => {
    setInstalled(new Set(installedNames));
  }, [installedNames]);

  const doSearch = useCallback(
    async (searchQuery: string, searchPage: number) => {
      const seq = searchSeqRef.current + 1;
      searchSeqRef.current = seq;
      setLoading(true);
      try {
        const res = await searchMarketSkills(searchQuery, searchPage, 20, lang);
        if (seq !== searchSeqRef.current) return;
        const accumulated =
          searchPage === 1
            ? res.results
            : [...resultsRef.current, ...res.results];
        resultsRef.current = accumulated;
        setResults(accumulated);
        setHasMore(res.has_more);
        setSearched(true);
      } catch {
        if (seq !== searchSeqRef.current) return;
        message.error(t("skills.marketSearchFailed"));
      } finally {
        if (seq === searchSeqRef.current) {
          setLoading(false);
        }
      }
    },
    [lang, t],
  );

  const handleSearch = useCallback(() => {
    setPage(1);
    void doSearch(query, 1);
  }, [query, doSearch]);

  const handleLoadMore = useCallback(() => {
    setPage((prev) => {
      const nextPage = prev + 1;
      void doSearch(query, nextPage);
      return nextPage;
    });
  }, [query, doSearch]);

  const handleInstall = useCallback(
    async (item: MarketSkillResult) => {
      if (
        installingRef.current.has(item.slug) ||
        installed.has(item.name) ||
        installedNames.includes(item.name)
      )
        return;
      installingRef.current = new Set(installingRef.current).add(item.slug);
      setInstalling((prev) => new Set(prev).add(item.slug));
      try {
        await importSkillFromUrl(item.source_url);
        setInstalled((prev) => new Set(prev).add(item.name));
        message.success(`${t("skills.importUrlSuccess")}: ${item.name}`);
        onInstalled();
      } catch (error) {
        message.error(
          error instanceof Error
            ? error.message
            : t("skills.importUrlFailed"),
        );
      } finally {
        const next = new Set(installingRef.current);
        next.delete(item.slug);
        installingRef.current = next;
        setInstalling((prev) => {
          const s = new Set(prev);
          s.delete(item.slug);
          return s;
        });
      }
    },
    [installed, installedNames, onInstalled, t],
  );

  // Run initial search when modal opens.
  useEffect(() => {
    if (open) {
      setQuery("");
      setResults([]);
      resultsRef.current = [];
      setSearched(false);
      setHasMore(false);
      setPage(1);
      void doSearch("", 1);
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <Modal
      open={open}
      title={
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <ShoppingBag size={18} />
          {t("skills.marketTitle")}
        </span>
      }
      onCancel={onClose}
      footer={null}
      width={780}
      centered
      bodyStyle={{ maxHeight: "70vh", overflow: "auto", padding: "16px 24px" }}
    >
      <div
        style={{
          fontSize: 12,
          color: "var(--color-text-tertiary)",
          marginBottom: 14,
          lineHeight: 1.5,
        }}
      >
        {t("skills.marketDesc")}
      </div>

      {/* Search bar */}
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <Input
          ref={inputRef as any}
          value={query}
          placeholder={t("skills.marketSearchPlaceholder")}
          prefix={<Search size={14} style={{ color: "var(--color-text-tertiary)" }} />}
          onChange={(e) => setQuery(e.target.value)}
          onPressEnter={handleSearch}
          style={{ flex: 1 }}
          allowClear
        />
        <Button
          type="primary"
          icon={<Search size={14} />}
          onClick={handleSearch}
          loading={loading}
        >
          {t("common.search")}
        </Button>
      </div>

      {/* Loading state */}
      {loading && results.length === 0 && (
        <div
          style={{
            display: "flex",
            justifyContent: "center",
            padding: "60px 0",
          }}
        >
          <Spin size="large" />
        </div>
      )}

      {/* Empty state */}
      {!loading && searched && results.length === 0 && (
        <div
          style={{
            textAlign: "center",
            padding: "60px 0",
            color: "var(--color-text-tertiary)",
            fontSize: 13,
          }}
        >
          <AppWindow size={36} style={{ marginBottom: 12, opacity: 0.4 }} />
          <div>{t("skills.marketNoResults")}</div>
        </div>
      )}

      {/* Results grid */}
      {results.length > 0 && (
        <>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
              gap: 10,
            }}
          >
            {results.map((item) => {
              const isInstalled =
                installed.has(item.name) || installedNames.includes(item.name);
              const isInstalling = installing.has(item.slug);
              const disabled = isInstalled || isInstalling;
              return (
                <div
                  key={`${item.source}-${item.slug}`}
                  style={{
                    border: "1px solid var(--color-border)",
                    borderRadius: 10,
                    padding: "12px 14px",
                    display: "flex",
                    flexDirection: "column",
                    gap: 6,
                    background: isInstalled
                      ? "var(--color-bg-layout)"
                      : undefined,
                  }}
                >
                  <div
                    style={{
                      fontSize: 13,
                      fontWeight: 600,
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                    }}
                  >
                    {item.icon_url ? (
                      <img
                        src={item.icon_url}
                        alt=""
                        style={{ width: 20, height: 20, borderRadius: 4 }}
                      />
                    ) : (
                      <Globe size={14} style={{ color: "var(--color-text-tertiary)" }} />
                    )}
                    <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {item.name}
                    </span>
                    <Button
                      size="small"
                      type={isInstalled ? "default" : "primary"}
                      disabled={disabled}
                      loading={isInstalling}
                      icon={isInstalled ? undefined : <Download size={13} />}
                      onClick={() => handleInstall(item)}
                      style={{ flexShrink: 0 }}
                    >
                      {isInstalled
                        ? t("skills.marketInstalled")
                        : isInstalling
                        ? t("skills.marketInstalling")
                        : t("skills.marketInstall")}
                    </Button>
                  </div>

                  {item.description && (
                    <div
                      style={{
                        fontSize: 11.5,
                        color: "var(--color-text-secondary)",
                        lineHeight: 1.5,
                        overflow: "hidden",
                        display: "-webkit-box",
                        WebkitLineClamp: 2,
                        WebkitBoxOrient: "vertical" as const,
                      }}
                    >
                      {item.description}
                    </div>
                  )}

                  <div
                    style={{
                      fontSize: 11,
                      color: "var(--color-text-tertiary)",
                      display: "flex",
                      alignItems: "center",
                      gap: 12,
                      flexWrap: "wrap",
                    }}
                  >
                    <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                      <ShoppingBag size={11} />
                      {sourceLabel(item.source, item.source_label)}
                    </span>
                    {item.author && (
                      <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                        <User size={11} />
                        {item.author}
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Load more */}
          {hasMore && (
            <div style={{ textAlign: "center", marginTop: 16 }}>
              <Button onClick={handleLoadMore} loading={loading}>
                {t("common.more")}
              </Button>
            </div>
          )}

          {/* Loading indicator for subsequent pages */}
          {loading && results.length > 0 && (
            <div style={{ textAlign: "center", marginTop: 12 }}>
              <Spin size="small" />
            </div>
          )}
        </>
      )}
    </Modal>
  );
}