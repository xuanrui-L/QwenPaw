import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  applySavedPositions,
  freeNodePosition,
  GRAPH_NODE_HEIGHT,
  GRAPH_NODE_WIDTH,
  GRAPH_PADDING,
  type GraphPoint,
} from "./narrativeGraphLayout";

export const storyMapStorageKey = (projectId: string) =>
  `creator:story-map:v1:${projectId}`;

export function readStoryPositions(
  projectId: string,
): Record<string, GraphPoint> {
  try {
    const value = JSON.parse(
      localStorage.getItem(storyMapStorageKey(projectId)) ?? "{}",
    );
    return Object.fromEntries(
      Object.entries(value).filter(([, p]) => {
        const point = p as GraphPoint;
        return (
          point &&
          Number.isFinite(point.x) &&
          Number.isFinite(point.y) &&
          point.x >= GRAPH_PADDING &&
          point.y >= GRAPH_PADDING &&
          point.x < 100_000 &&
          point.y < 100_000
        );
      }),
    ) as Record<string, GraphPoint>;
  } catch {
    return {};
  }
}

interface Gesture {
  pointerId: number;
  kind: "node" | "pan";
  id?: string;
  start: GraphPoint;
  origin: GraphPoint;
  scroll: GraphPoint;
  moved: boolean;
  position?: GraphPoint;
}

export function useNarrativeGraphViewport(
  projectId: string,
  automatic: Map<string, GraphPoint>,
  expanded = false,
) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const [saved, setSaved] = useState(() => readStoryPositions(projectId));
  const [scale, setScale] = useState(1);
  const [dragged, setDragged] = useState<{
    id: string;
    point: GraphPoint;
  } | null>(null);
  const [panning, setPanning] = useState(false);
  const gesture = useRef<Gesture | null>(null);
  const suppressClick = useRef(false);
  const stablePositions = useMemo(
    () => applySavedPositions(automatic, saved),
    [automatic, saved],
  );
  const positions = useMemo(() => {
    if (!dragged || !stablePositions.has(dragged.id)) return stablePositions;
    return new Map(stablePositions).set(dragged.id, dragged.point);
  }, [stablePositions, dragged]);
  const width = Math.max(
    640,
    ...[...positions.values()].map(
      (p) => p.x + GRAPH_NODE_WIDTH + GRAPH_PADDING,
    ),
  );
  const height = Math.max(
    480,
    ...[...positions.values()].map(
      (p) => p.y + GRAPH_NODE_HEIGHT + GRAPH_PADDING,
    ),
  );
  const pendingView = useRef<{ left: number; top: number } | null>(null);
  const lastView = useRef({ left: 0, top: 0 });
  // The expanded portal replaces the viewport DOM, but keeps the current view.
  useLayoutEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    viewport.scrollLeft = lastView.current.left;
    viewport.scrollTop = lastView.current.top;
  }, [expanded]);
  function rememberView() {
    const viewport = viewportRef.current;
    if (viewport)
      lastView.current = { left: viewport.scrollLeft, top: viewport.scrollTop };
  }

  function persist(next: Record<string, GraphPoint>) {
    setSaved(next);
    // Browser-only presentation preference. Never patch the project/WorkGraph.
    try {
      localStorage.setItem(storyMapStorageKey(projectId), JSON.stringify(next));
    } catch {
      /* Private mode / quota: keep working in memory. */
    }
  }

  const zoom = useCallback(
    (requested: number, anchor?: GraphPoint) => {
      const viewport = viewportRef.current;
      if (!viewport) return;
      const next = Math.min(1.5, Math.max(0.1, requested));
      const at = anchor ?? {
        x: viewport.clientWidth / 2,
        y: viewport.clientHeight / 2,
      };
      pendingView.current = {
        left: ((viewport.scrollLeft + at.x) * next) / scale - at.x,
        top: ((viewport.scrollTop + at.y) * next) / scale - at.y,
      };
      setScale(next);
    },
    [scale],
  );

  useLayoutEffect(() => {
    if (pendingView.current && viewportRef.current) {
      viewportRef.current.scrollLeft = pendingView.current.left;
      viewportRef.current.scrollTop = pendingView.current.top;
      pendingView.current = null;
    }
  }, [scale]);

  function fit() {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const next = Math.max(
      0.1,
      Math.min(
        1,
        (viewport.clientWidth - 32) / width,
        (viewport.clientHeight - 32) / height,
      ),
    );
    pendingView.current = { left: 0, top: 0 };
    setScale(next);
    viewport.scrollLeft = 0;
    viewport.scrollTop = 0;
  }

  function focusNode(id: string) {
    const viewport = viewportRef.current,
      point = positions.get(id);
    if (!viewport || !point) return;
    // A node jump should always land at a readable size after overview mode.
    const next = Math.max(scale, 0.75);
    const view = {
      left: (point.x + GRAPH_NODE_WIDTH / 2) * next - viewport.clientWidth / 2,
      top: (point.y + GRAPH_NODE_HEIGHT / 2) * next - viewport.clientHeight / 2,
    };
    pendingView.current = view;
    setScale(next);
    viewport.scrollLeft = view.left;
    viewport.scrollTop = view.top;
  }

  const initialized = useRef(false);
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !positions.size || initialized.current) return;
    const initialize = () => {
      if (!viewport.clientHeight || initialized.current) return;
      const first = positions.values().next().value as GraphPoint;
      viewport.scrollTop = Math.max(
        0,
        first.y + GRAPH_NODE_HEIGHT / 2 - viewport.clientHeight / 2,
      );
      initialized.current = true;
    };
    initialize();
    const observer = new ResizeObserver(initialize);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, [positions, expanded]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    function wheel(event: WheelEvent) {
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        const bounds = viewport!.getBoundingClientRect();
        zoom(scale * Math.exp(-event.deltaY * 0.003), {
          x: event.clientX - bounds.left,
          y: event.clientY - bounds.top,
        });
      } else if (event.shiftKey) {
        event.preventDefault();
        const unit =
          event.deltaMode === 1
            ? 16
            : event.deltaMode === 2
            ? viewport!.clientWidth
            : 1;
        // Some browsers already move Shift-wheel into deltaX; accept either.
        viewport!.scrollLeft +=
          (Math.abs(event.deltaX) > Math.abs(event.deltaY)
            ? event.deltaX
            : event.deltaY) * unit;
      }
    }
    viewport.addEventListener("wheel", wheel, { passive: false });
    return () => viewport.removeEventListener("wheel", wheel);
  }, [scale, zoom, expanded]);

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0 || !event.isPrimary) return;
    const target = event.target as HTMLElement;
    if (target.closest("[data-graph-action], [data-graph-edge], input, select"))
      return;
    const node = target.closest<HTMLElement>("[data-blueprint-node]");
    const id = node?.dataset.blueprintNode;
    const viewport = viewportRef.current!;
    suppressClick.current = false;
    gesture.current = {
      kind: id ? "node" : "pan",
      pointerId: event.pointerId,
      id,
      start: { x: event.clientX, y: event.clientY },
      origin: id ? positions.get(id)! : { x: 0, y: 0 },
      scroll: { x: viewport.scrollLeft, y: viewport.scrollTop },
      moved: false,
    };
    // Capture on the original node so a simple click still reaches its handler.
    (node ?? viewport).setPointerCapture(event.pointerId);
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const active = gesture.current,
      viewport = viewportRef.current;
    if (!active || event.pointerId !== active.pointerId || !viewport) return;
    const dx = event.clientX - active.start.x,
      dy = event.clientY - active.start.y;
    if (!active.moved && Math.hypot(dx, dy) < 5) return;
    active.moved = true;
    suppressClick.current = true;
    if (active.kind === "pan") {
      setPanning(true);
      viewport.scrollLeft = active.scroll.x - dx;
      viewport.scrollTop = active.scroll.y - dy;
    } else {
      const point = {
        x: Math.max(
          GRAPH_PADDING,
          active.origin.x +
            (dx + viewport.scrollLeft - active.scroll.x) / scale,
        ),
        y: Math.max(
          GRAPH_PADDING,
          active.origin.y + (dy + viewport.scrollTop - active.scroll.y) / scale,
        ),
      };
      active.position = point;
      setDragged({ id: active.id!, point });
    }
  }

  function finishGesture(event: ReactPointerEvent<HTMLDivElement>) {
    const active = gesture.current;
    if (!active || event.pointerId !== active.pointerId) return;
    if (
      event.type !== "pointercancel" &&
      active.moved &&
      active.id &&
      active.position &&
      stablePositions.has(active.id)
    ) {
      const next = new Map(stablePositions);
      next.delete(active.id);
      next.set(
        active.id,
        freeNodePosition(active.position, [...next.values()]),
      );
      // Freeze the other visible nodes as well, so later snapshots adding a
      // branch cannot undo the arrangement the user just made.
      persist(Object.fromEntries(next));
    }
    gesture.current = null;
    setDragged(null);
    setPanning(false);
  }

  function onClickCapture(event: React.MouseEvent) {
    if (suppressClick.current) {
      event.preventDefault();
      event.stopPropagation();
      suppressClick.current = false;
    }
  }

  function resetLayout() {
    persist({});
    setDragged(null);
  }

  return {
    viewportRef,
    positions,
    width,
    height,
    scale,
    zoom,
    fit,
    focusNode,
    resetLayout,
    rememberView,
    draggedId: dragged?.id,
    panning,
    handlers: {
      onPointerDown,
      onPointerMove,
      onPointerUp: finishGesture,
      onPointerCancel: finishGesture,
      onLostPointerCapture: finishGesture,
      onClickCapture,
    },
  };
}
