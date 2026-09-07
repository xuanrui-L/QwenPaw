import { describe, expect, it } from "vitest";
import {
  isSystemVersionReviewOperation,
  pendingUserReviewOperations,
  userReviewOperations,
} from "@/lib/fileProjectReviewDecisions";
import { makeReviewOperation, makeReviewRecord } from "@/test/agentFixtures";
import type { FileProjectReviewOperation } from "@/contracts/creator";

const id = "snapshot:timeline:main:8";
function automatic(overrides: Partial<FileProjectReviewOperation> = {}) {
  return makeReviewOperation({
    kind: "create",
    json_pointer: `/timelines/items/${id}`,
    before: null,
    after: {
      timeline_id: id,
      description: "自动快照：修改前的时间轴副本",
      elements_by_id: {},
    },
    ...overrides,
  });
}

describe("system version review decisions", () => {
  it("recognizes the actual automatic snapshot and pure history-order pair without mutating them", () => {
    const review = makeReviewRecord({
      operations: [
        automatic(),
        makeReviewOperation({
          json_pointer: "/timelines/order",
          kind: "reorder",
          before: ["timeline:main"],
          after: ["timeline:main", id],
        }),
      ],
    });
    const original = structuredClone(review);
    expect(review.operations.every(isSystemVersionReviewOperation)).toBe(true);
    expect(userReviewOperations(review)).toEqual([]);
    expect(pendingUserReviewOperations(review)).toEqual([]);
    expect(review).toEqual(original);
  });

  it.each([
    ["an explicitly edited snapshot", { kind: "update" }],
    ["a deleted snapshot", { kind: "delete" }],
    [
      "an unknown snapshot description",
      { after: { timeline_id: id, description: "用户保存的备选方案" } },
    ],
    [
      "a missing producer identity",
      { after: { description: "自动快照：修改前的时间轴副本" } },
    ],
    [
      "another snapshot identity",
      {
        after: {
          timeline_id: "snapshot:other",
          description: "自动快照：修改前的时间轴副本",
        },
      },
    ],
    [
      "a nested snapshot field",
      { json_pointer: `/timelines/items/${id}/description` },
    ],
    ["a create with existing content", { before: { timeline_id: id } }],
    [
      "an ordinary live timeline",
      { json_pointer: "/timelines/items/timeline:main" },
    ],
    [
      "an artifact bookkeeping operation",
      { json_pointer: "/assets/artifact_versions_by_id/result" },
    ],
  ] satisfies [string, Partial<FileProjectReviewOperation>][])(
    "keeps %s reviewable",
    (_label, overrides) => {
      expect(isSystemVersionReviewOperation(automatic(overrides))).toBe(false);
    },
  );

  it.each([
    [
      ["timeline:main", "timeline:second"],
      ["timeline:second", "timeline:main", id],
    ],
    [["timeline:main"], ["timeline:main", "timeline:new", id]],
    [
      ["timeline:main", "timeline:second"],
      ["timeline:main", id],
    ],
    [["timeline:main"], ["timeline:main"]],
    [
      ["timeline:main", 1],
      ["timeline:main", id],
    ],
    [null, [id]],
  ])("does not hide live or unproven order changes %#", (before, after) => {
    expect(
      isSystemVersionReviewOperation(
        makeReviewOperation({
          json_pointer: "/timelines/order",
          kind: "reorder",
          before,
          after,
        }),
      ),
    ).toBe(false);
  });

  it("retains real content and artifact decisions in a mixed review while omitting resolved content from pending counts", () => {
    const content = makeReviewOperation({
      operation_id: "content",
      json_pointer: "/description",
    });
    const media = makeReviewOperation({
      operation_id: "media",
      json_pointer: "/assets/artifact_slots_by_id/result/selected_version_id",
    });
    const accepted = makeReviewOperation({
      operation_id: "accepted",
      decision: "ACCEPTED",
    });
    const review = makeReviewRecord({
      operations: [automatic(), content, media, accepted],
    });
    expect(userReviewOperations(review)).toEqual([content, media, accepted]);
    expect(pendingUserReviewOperations(review)).toEqual([content, media]);
  });
});
