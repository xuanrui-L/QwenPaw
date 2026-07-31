/** Inspiration example cards backed by plugin-bundled built-in Projects. */

import { useEffect, useState } from "react";
import { message } from "antd";
import { Loader2 } from "lucide-react";
import type { InspirationExampleSummary } from "@/contracts/creator";
import { listInspirationExamples, openInspirationExample } from "@/api/creator";
import { useRouter } from "@/routing/navigation";

export default function InspirationExamples() {
  const router = useRouter();
  const [examples, setExamples] = useState<InspirationExampleSummary[]>([]);
  const [openingId, setOpeningId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listInspirationExamples()
      .then((data) => {
        if (!cancelled) setExamples(data.items ?? []);
      })
      .catch(() => {
        // No bundled examples (or an older backend): keep the section hidden.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleOpen = async (example: InspirationExampleSummary) => {
    if (openingId !== null) return;
    setOpeningId(example.id);
    try {
      const opened = await openInspirationExample(example.id);
      router.push(`/project/${opened.projectId}/plan`);
    } catch {
      message.error("打开灵感示例失败，请稍后重试");
      setOpeningId(null);
    }
  };

  if (examples.length === 0) return null;
  return (
    <div className="w-full">
      <p className="mb-2 text-sm leading-6 tracking-[0.4px] text-[#808080]">
        灵感示例
      </p>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {examples.map((example) => (
          <button
            key={example.id}
            type="button"
            disabled={openingId !== null}
            onClick={() => void handleOpen(example)}
            className="cursor-pointer rounded-lg border border-[#eae9e7] bg-white/90 p-4 text-left backdrop-blur-sm transition-colors hover:border-[var(--color-accent)] disabled:cursor-default disabled:opacity-70"
          >
            <p className="flex items-center gap-2 text-sm font-medium leading-6 tracking-[0.4px] text-[#474a52]">
              {example.title}
              {openingId === example.id && (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--color-accent)]" />
              )}
            </p>
            <p className="mt-2 truncate text-xs leading-[17px] text-[#808080]">
              {example.description}
            </p>
          </button>
        ))}
      </div>
    </div>
  );
}
