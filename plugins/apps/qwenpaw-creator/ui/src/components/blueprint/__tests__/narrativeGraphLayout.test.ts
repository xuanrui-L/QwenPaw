import { describe, expect, it } from "vitest";
import type { NarrativeEdgeDocument } from "@/contracts/creator";
import {
  applySavedPositions,
  freeNodePosition,
  GRAPH_NODE_HEIGHT,
  GRAPH_NODE_WIDTH,
  layoutStoryNodes,
  nodesOverlap,
  routeStoryEdges,
  type GraphPoint,
} from "../narrativeGraphLayout";

const edge = (
  source: string,
  target: string,
  i = 0,
): NarrativeEdgeDocument => ({
  edge_id: `${source}-${target}-${i}`,
  source_timeline_id: source,
  target_timeline_id: target,
  label: `选择 ${i}`,
  prompt: "下一步？",
});

// Same 37-node / 62-link branching and merging density as the reported project.
const adjacency = [
  [1, 2],
  [3, 4],
  [3, 4],
  [5, 6],
  [5, 6],
  [7, 8],
  [7, 8],
  [9, 10],
  [9, 10],
  [11, 12],
  [13, 14],
  [15, 16],
  [17, 18],
  [19, 20],
  [21, 22],
  [23, 24],
  [23, 24],
  [23, 24],
  [23, 24],
  [23, 26],
  [23, 26],
  [23, 24, 25],
  [23, 24, 25],
  [27, 34, 32],
  [30, 29, 31],
  [28, 29, 32],
  [28, 33, 35],
  [],
  [],
  [],
  [],
  [36],
  [36],
  [],
  [],
  [],
  [],
];
const ids = adjacency.map((_, i) => `n${i}`);
const edges = adjacency.flatMap((targets, source) =>
  targets.map((target, i) => edge(`n${source}`, `n${target}`, i)),
);

function expectNoOverlap(positions: Map<string, GraphPoint>) {
  const points = [...positions.values()];
  points.forEach((a, i) =>
    points
      .slice(i + 1)
      .forEach((b) => expect(nodesOverlap(a, b, 0)).toBe(false)),
  );
}

function expectRoutesClear(
  links: NarrativeEdgeDocument[],
  positions: Map<string, GraphPoint>,
) {
  const routes = routeStoryEdges(links, positions);
  expect(routes).toHaveLength(links.length);
  for (const route of routes) {
    expect(route.path).not.toMatch(/NaN|Infinity/);
    for (let i = 1; i < route.points.length; i++) {
      const a = route.points[i - 1],
        b = route.points[i];
      expect(a.x === b.x || a.y === b.y).toBe(true);
      for (const p of positions.values()) {
        const through =
          a.x === b.x
            ? a.x > p.x &&
              a.x < p.x + GRAPH_NODE_WIDTH &&
              Math.max(a.y, b.y) > p.y &&
              Math.min(a.y, b.y) < p.y + GRAPH_NODE_HEIGHT
            : a.y > p.y &&
              a.y < p.y + GRAPH_NODE_HEIGHT &&
              Math.max(a.x, b.x) > p.x &&
              Math.min(a.x, b.x) < p.x + GRAPH_NODE_WIDTH;
        expect(through, `${route.edge.edge_id} passes through a card`).toBe(
          false,
        );
      }
    }
  }
  return routes;
}

describe("story map layout", () => {
  it("keeps all 37 nodes separate and routes all 62 choices outside cards", () => {
    expect(ids).toHaveLength(37);
    expect(edges).toHaveLength(62);
    const positions = layoutStoryNodes(ids, edges);
    expectNoOverlap(positions);
    const routes = expectRoutesClear(edges, positions);
    // Dense merge paths have separate source ports, destination ports and lanes.
    const intoMerge = routes.filter((r) => r.edge.target_timeline_id === "n23");
    expect(new Set(intoMerge.map((r) => r.target.y)).size).toBe(
      intoMerge.length,
    );
    expect(new Set(intoMerge.map((r) => r.points[2].x)).size).toBe(
      intoMerge.length,
    );
    expect(
      layoutStoryNodes(
        ids,
        edges.map((e) => ({ ...e, label: "修改文案" })),
      ),
    ).toEqual(positions);
  });

  it("reduces a crossed diamond and centres its single root", () => {
    const positions = layoutStoryNodes(
      ["root", "a", "b", "c", "d"],
      [edge("root", "a"), edge("root", "b"), edge("a", "d"), edge("b", "c")],
    );
    expect(Math.sign(positions.get("a")!.y - positions.get("b")!.y)).toBe(
      Math.sign(positions.get("d")!.y - positions.get("c")!.y),
    );
    expect(positions.get("root")!.y).toBe(
      (positions.get("a")!.y + positions.get("b")!.y) / 2,
    );
  });

  it("handles loops, disconnected nodes and missing endpoints without runaway columns", () => {
    const links = [
      edge("a", "b"),
      edge("b", "a"),
      edge("b", "b"),
      edge("missing", "a"),
    ];
    const positions = layoutStoryNodes(["a", "b", "isolated"], links);
    expectNoOverlap(positions);
    expect(Math.max(...[...positions.values()].map((p) => p.x))).toBeLessThan(
      1200,
    );
    expectRoutesClear(links.slice(0, 3), positions);
    expect(routeStoryEdges(links, positions)).toHaveLength(3);
  });

  it("routes a skipped layer and a backwards dragged branch around intervening nodes", () => {
    const positions = new Map([
      ["a", { x: 48, y: 200 }],
      ["obstacle", { x: 450, y: 200 }],
      ["b", { x: 900, y: 200 }],
    ]);
    expectRoutesClear([edge("a", "b"), edge("b", "a")], positions);
  });

  it("preserves browser positions and places arriving nodes without collisions", () => {
    const automatic = layoutStoryNodes(ids, edges);
    const saved = Object.fromEntries(automatic);
    saved.n15 = freeNodePosition(
      { x: saved.n15.x + 80, y: saved.n15.y - 40 },
      [...automatic].filter(([id]) => id !== "n15").map(([, p]) => p),
    );
    const updated = layoutStoryNodes(
      [...ids, "new"],
      [...edges, edge("n14", "new")],
    );
    const applied = applySavedPositions(updated, saved);
    for (const id of ids) expect(applied.get(id)).toEqual(saved[id]);
    expectNoOverlap(applied);
    expectRoutesClear(edges, applied);
    const dropped = freeNodePosition(
      applied.get("n16")!,
      [...applied].filter(([id]) => id !== "n15").map(([, p]) => p),
    );
    expect(nodesOverlap(dropped, applied.get("n16")!)).toBe(false);
  });
});
