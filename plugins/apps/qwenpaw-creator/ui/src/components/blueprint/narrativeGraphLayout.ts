import type { NarrativeEdgeDocument } from "@/contracts/creator";

export interface GraphPoint {
  x: number;
  y: number;
}
export const GRAPH_NODE_WIDTH = 256;
export const GRAPH_NODE_HEIGHT = 200;
export const GRAPH_PADDING = 48;
const NODE_GAP = 40;
const COLUMN_GAP = 208;
const ROW_GAP = 64;

/** Layout depends only on story topology, never on generation progress or copy. */
export function layoutStoryNodes(
  ids: string[],
  edges: Pick<
    NarrativeEdgeDocument,
    "source_timeline_id" | "target_timeline_id"
  >[],
): Map<string, GraphPoint> {
  const outgoing = new Map(ids.map((id) => [id, new Set<string>()]));
  const incoming = new Map(ids.map((id) => [id, new Set<string>()]));
  for (const edge of edges) {
    if (
      !outgoing.has(edge.source_timeline_id) ||
      !incoming.has(edge.target_timeline_id)
    )
      continue;
    outgoing.get(edge.source_timeline_id)!.add(edge.target_timeline_id);
    incoming.get(edge.target_timeline_id)!.add(edge.source_timeline_id);
  }
  // Ignore DFS back edges for ranking only. Loops remain visible in the map;
  // they must not keep increasing the ranks (and canvas width) on every pass.
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const order: string[] = [];
  const backEdges = new Set<string>();
  function visit(id: string) {
    if (visited.has(id)) return;
    visiting.add(id);
    for (const next of outgoing.get(id)!) {
      if (visiting.has(next)) backEdges.add(JSON.stringify([id, next]));
      else visit(next);
    }
    visiting.delete(id);
    visited.add(id);
    order.push(id);
  }
  ids.forEach(visit);
  const ranks = new Map(ids.map((id) => [id, 0]));
  for (const id of order.reverse()) {
    for (const next of outgoing.get(id)!) {
      if (!backEdges.has(JSON.stringify([id, next]))) {
        ranks.set(next, Math.max(ranks.get(next)!, ranks.get(id)! + 1));
      }
    }
  }
  const layers: string[][] = [];
  for (const id of ids) (layers[ranks.get(id)!] ??= []).push(id);
  const widest = Math.max(1, ...layers.map((layer) => layer.length));
  const rows = new Map<string, number>();
  function updateRows() {
    layers.forEach((layer) =>
      layer.forEach((id, index) =>
        rows.set(id, index + (widest - layer.length) / 2),
      ),
    );
  }
  updateRows();
  // Alternating barycentre sweeps group connected branches on both sides of
  // merge points. Stable ties preserve the writer's original node order.
  for (let pass = 0; pass < 6; pass += 1) {
    for (const direction of [1, -1]) {
      const sweep = direction === 1 ? layers : [...layers].reverse();
      for (const layer of sweep) {
        const neighbours = direction === 1 ? incoming : outgoing;
        const centre = (id: string) => {
          const relevant = [...neighbours.get(id)!].filter((next) =>
            direction === 1
              ? ranks.get(next)! < ranks.get(id)!
              : ranks.get(next)! > ranks.get(id)!,
          );
          return relevant.length
            ? relevant.reduce((sum, next) => sum + rows.get(next)!, 0) /
                relevant.length
            : rows.get(id)!;
        };
        layer.sort((a, b) => centre(a) - centre(b));
        updateRows();
      }
    }
  }
  return new Map(
    ids.map((id) => [
      id,
      {
        x: GRAPH_PADDING + ranks.get(id)! * (GRAPH_NODE_WIDTH + COLUMN_GAP),
        y: GRAPH_PADDING + rows.get(id)! * (GRAPH_NODE_HEIGHT + ROW_GAP),
      },
    ]),
  );
}

export function nodesOverlap(a: GraphPoint, b: GraphPoint, gap = NODE_GAP) {
  return (
    a.x < b.x + GRAPH_NODE_WIDTH + gap &&
    a.x + GRAPH_NODE_WIDTH + gap > b.x &&
    a.y < b.y + GRAPH_NODE_HEIGHT + gap &&
    a.y + GRAPH_NODE_HEIGHT + gap > b.y
  );
}

/** Resolve a dropped node locally, without moving any other saved position. */
export function freeNodePosition(
  wanted: GraphPoint,
  occupied: GraphPoint[],
): GraphPoint {
  const start = {
    x: Math.max(GRAPH_PADDING, wanted.x),
    y: Math.max(GRAPH_PADDING, wanted.y),
  };
  if (occupied.every((other) => !nodesOverlap(start, other))) return start;
  const xs = new Set([start.x, GRAPH_PADDING]);
  const ys = new Set([start.y, GRAPH_PADDING]);
  for (const other of occupied) {
    xs.add(Math.max(GRAPH_PADDING, other.x - GRAPH_NODE_WIDTH - NODE_GAP));
    xs.add(other.x + GRAPH_NODE_WIDTH + NODE_GAP);
    ys.add(Math.max(GRAPH_PADDING, other.y - GRAPH_NODE_HEIGHT - NODE_GAP));
    ys.add(other.y + GRAPH_NODE_HEIGHT + NODE_GAP);
  }
  let best = {
    x: start.x,
    y: Math.max(
      start.y,
      ...occupied.map((p) => p.y + GRAPH_NODE_HEIGHT + NODE_GAP),
    ),
  };
  let distance = Infinity;
  for (const x of xs)
    for (const y of ys) {
      const d = (x - start.x) ** 2 + (y - start.y) ** 2;
      if (
        d < distance &&
        occupied.every((other) => !nodesOverlap({ x, y }, other))
      ) {
        best = { x, y };
        distance = d;
      }
    }
  return best;
}

export function applySavedPositions(
  automatic: Map<string, GraphPoint>,
  saved: Record<string, GraphPoint>,
) {
  const result = new Map<string, GraphPoint>();
  // Saved nodes take precedence, including when the Agent adds another node.
  for (const id of automatic.keys())
    if (Object.hasOwn(saved, id))
      result.set(id, freeNodePosition(saved[id], [...result.values()]));
  for (const [id, point] of automatic)
    if (!result.has(id))
      result.set(id, freeNodePosition(point, [...result.values()]));
  return result;
}

export interface GraphRoute {
  edge: NarrativeEdgeDocument;
  points: GraphPoint[];
  path: string;
  source: GraphPoint;
  target: GraphPoint;
}

function roundedPath(points: GraphPoint[]) {
  let path = `M ${points[0].x} ${points[0].y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const a = points[i - 1],
      b = points[i],
      c = points[i + 1];
    const before = Math.hypot(b.x - a.x, b.y - a.y);
    const after = Math.hypot(c.x - b.x, c.y - b.y);
    if (!before || !after) continue;
    const radius = Math.min(8, before / 2, after / 2);
    path += ` L ${b.x + ((a.x - b.x) * radius) / before} ${
      b.y + ((a.y - b.y) * radius) / before
    }`;
    path += ` Q ${b.x} ${b.y} ${b.x + ((c.x - b.x) * radius) / after} ${
      b.y + ((c.y - b.y) * radius) / after
    }`;
  }
  const end = points[points.length - 1];
  return `${path} L ${end.x} ${end.y}`;
}

function segmentClear(a: GraphPoint, b: GraphPoint, obstacles: GraphPoint[]) {
  return obstacles.every((p) =>
    a.x === b.x
      ? a.x <= p.x - 12 ||
        a.x >= p.x + GRAPH_NODE_WIDTH + 12 ||
        Math.max(a.y, b.y) <= p.y - 12 ||
        Math.min(a.y, b.y) >= p.y + GRAPH_NODE_HEIGHT + 12
      : a.y <= p.y - 12 ||
        a.y >= p.y + GRAPH_NODE_HEIGHT + 12 ||
        Math.max(a.x, b.x) <= p.x - 12 ||
        Math.min(a.x, b.x) >= p.x + GRAPH_NODE_WIDTH + 12,
  );
}

/** Orthogonal visibility grid used only for a loop, skipped layer or moved node
 * whose normal column channel is obstructed. Adjacent layers use the fast path. */
function detour(
  start: GraphPoint,
  end: GraphPoint,
  obstacles: GraphPoint[],
): GraphPoint[] {
  const xs = [
    ...new Set([
      start.x,
      end.x,
      ...obstacles.flatMap((p) => [p.x - 16, p.x + GRAPH_NODE_WIDTH + 16]),
    ]),
  ].sort((a, b) => a - b);
  const ys = [
    ...new Set([
      start.y,
      end.y,
      ...obstacles.flatMap((p) => [p.y - 16, p.y + GRAPH_NODE_HEIGHT + 16]),
    ]),
  ].sort((a, b) => a - b);
  const key = (x: number, y: number) => y * xs.length + x;
  const from = key(xs.indexOf(start.x), ys.indexOf(start.y));
  const to = key(xs.indexOf(end.x), ys.indexOf(end.y));
  const point = (k: number) => ({
    x: xs[k % xs.length],
    y: ys[Math.floor(k / xs.length)],
  });
  const distance = new Map([[from, 0]]);
  const previous = new Map<number, number>();
  const open = new Set([from]);
  while (open.size) {
    let current = -1,
      score = Infinity;
    for (const k of open) {
      const p = point(k);
      const cost =
        distance.get(k)! + Math.abs(p.x - end.x) + Math.abs(p.y - end.y);
      if (cost < score) {
        current = k;
        score = cost;
      }
    }
    if (current === to) {
      const path = [end];
      while (previous.has(current)) {
        current = previous.get(current)!;
        path.unshift(point(current));
      }
      // Remove collinear grid vertices before rounding corners.
      return path.filter(
        (p, i) =>
          i === 0 ||
          i === path.length - 1 ||
          !(
            (path[i - 1].x === p.x && p.x === path[i + 1].x) ||
            (path[i - 1].y === p.y && p.y === path[i + 1].y)
          ),
      );
    }
    open.delete(current);
    const x = current % xs.length,
      y = Math.floor(current / xs.length),
      a = point(current);
    for (const [nx, ny] of [
      [x - 1, y],
      [x + 1, y],
      [x, y - 1],
      [x, y + 1],
    ]) {
      if (nx < 0 || ny < 0 || nx >= xs.length || ny >= ys.length) continue;
      const next = key(nx, ny),
        b = point(next);
      if (!segmentClear(a, b, obstacles)) continue;
      const cost =
        distance.get(current)! + Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
      if (cost < (distance.get(next) ?? Infinity)) {
        distance.set(next, cost);
        previous.set(next, current);
        open.add(next);
      }
    }
  }
  return [start, { x: start.x, y: end.y }, end];
}

export function routeStoryEdges(
  edges: NarrativeEdgeDocument[],
  positions: Map<string, GraphPoint>,
): GraphRoute[] {
  const valid = edges.filter(
    (e) =>
      positions.has(e.source_timeline_id) &&
      positions.has(e.target_timeline_id),
  );
  const outgoing = new Map<string, NarrativeEdgeDocument[]>();
  const incoming = new Map<string, NarrativeEdgeDocument[]>();
  const channels = new Map<string, NarrativeEdgeDocument[]>();
  const channelKey = (edge: NarrativeEdgeDocument) =>
    `${positions.get(edge.source_timeline_id)!.x}:${
      positions.get(edge.target_timeline_id)!.x
    }`;
  for (const edge of valid) {
    const channel = channelKey(edge);
    channels.set(channel, [...(channels.get(channel) ?? []), edge]);
    outgoing.set(edge.source_timeline_id, [
      ...(outgoing.get(edge.source_timeline_id) ?? []),
      edge,
    ]);
    incoming.set(edge.target_timeline_id, [
      ...(incoming.get(edge.target_timeline_id) ?? []),
      edge,
    ]);
  }
  for (const group of outgoing.values())
    group.sort(
      (a, b) =>
        positions.get(a.target_timeline_id)!.y -
        positions.get(b.target_timeline_id)!.y,
    );
  for (const group of incoming.values())
    group.sort(
      (a, b) =>
        positions.get(a.source_timeline_id)!.y -
        positions.get(b.source_timeline_id)!.y,
    );
  for (const group of channels.values())
    group.sort(
      (a, b) =>
        positions.get(a.source_timeline_id)!.y -
          positions.get(b.source_timeline_id)!.y ||
        positions.get(a.target_timeline_id)!.y -
          positions.get(b.target_timeline_id)!.y,
    );
  const obstacles = [...positions.values()];
  return valid.map((edge) => {
    const a = positions.get(edge.source_timeline_id)!,
      b = positions.get(edge.target_timeline_id)!;
    const outs = outgoing.get(edge.source_timeline_id)!,
      ins = incoming.get(edge.target_timeline_id)!;
    const port = (list: NarrativeEdgeDocument[]) =>
      24 +
      ((GRAPH_NODE_HEIGHT - 48) * (list.indexOf(edge) + 1)) / (list.length + 1);
    const source = { x: a.x + GRAPH_NODE_WIDTH, y: a.y + port(outs) };
    const target = { x: b.x, y: b.y + port(ins) };
    const start = { x: source.x + 16, y: source.y },
      end = { x: target.x - 16, y: target.y };
    const channel = channels.get(channelKey(edge))!;
    const lane =
      start.x +
      (end.x - start.x) *
        (0.1 + (0.8 * (channel.indexOf(edge) + 1)) / (channel.length + 1));
    let middle = [start, { x: lane, y: start.y }, { x: lane, y: end.y }, end];
    if (
      end.x < start.x ||
      middle.some((p, i) => i > 0 && !segmentClear(middle[i - 1], p, obstacles))
    )
      middle = detour(start, end, obstacles);
    const points = [source, ...middle, target];
    return { edge, source, target, points, path: roundedPath(points) };
  });
}
