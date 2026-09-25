import { useEffect, useRef, useState, type ReactNode } from "react";

/** Preview a real device viewport, scaled to fit the editor pane. */
export default function DesignViewport({
  children,
  mobile = false,
  active = true,
}: {
  children: ReactNode;
  mobile?: boolean;
  active?: boolean;
}) {
  const root = useRef<HTMLDivElement>(null);
  const [available, setAvailable] = useState(0);
  useEffect(() => {
    if (!active || !root.current) return;
    const element = root.current;
    const measure = () => setAvailable(element.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [active]);
  if (!active) return children;
  const width = mobile ? 390 : 1280;
  const height = 720;
  const scale = available > 0 ? Math.min(1, available / width) : 0;
  return (
    <div
      ref={root}
      className="relative mx-auto w-full overflow-hidden rounded-lg border border-[var(--color-border)]"
      style={{
        aspectRatio: `${width} / ${height}`,
        maxWidth: mobile ? width : undefined,
      }}
      data-design-viewport={mobile ? "mobile" : "desktop"}
    >
      <div
        style={{
          width,
          height,
          position: "absolute",
          left: "50%",
          transform: `translateX(-50%) scale(${scale})`,
          transformOrigin: "top center",
        }}
      >
        {children}
      </div>
    </div>
  );
}
