import { describe, expect, it } from "vitest";
import type { ProjectDocument } from "@/contracts/creator";
import { fileReviewPresentation } from "@/lib/fileProjectReviewPresentation";
import { makeReviewOperation } from "@/test/agentFixtures";

const project = {
  timelines: {
    items: {
      "timeline:main": {
        title: "色彩练习",
        elements_by_id: { "seg:0-3": { label: "色彩启幕" } },
      },
    },
  },
} as unknown as ProjectDocument;

describe("public file project review presentation", () => {
  it("shows the real renamed segment as public text without changing its pointer", () => {
    const operation = makeReviewOperation({
      json_pointer:
        "/timelines/items/timeline:main/elements_by_id/seg:0-3/label",
      ui_locator: { elementId: "seg:0-3" },
      before: "色彩练习 · 前半段",
      after: "色彩启幕",
    });
    const original = structuredClone(operation);
    expect(fileReviewPresentation(operation, project)).toMatchObject({
      title: "色彩启幕 · 名称",
      preview: "色彩练习 · 前半段 → 色彩启幕",
      beforeText: "色彩练习 · 前半段",
      afterText: "色彩启幕",
      hasTextDiff: true,
    });
    expect(operation).toEqual(original);
  });

  it("describes real timeline snapshot creation and ordering without serializing structures", () => {
    const created = fileReviewPresentation(
      makeReviewOperation({
        kind: "create",
        json_pointer: "/timelines/items/snapshot:timeline:main:1",
        before: null,
        after: {
          title: "色彩练习",
          name: "快照 · 色彩练习",
          color_grade: "none",
          elements_by_id: { "seg:0-3": {} },
        },
      }),
      project,
    );
    expect(created).toMatchObject({
      title: "版本记录",
      preview: "已保存修改前版本",
      hasTextDiff: false,
      canInspect: false,
    });
    const order = fileReviewPresentation(
      makeReviewOperation({
        kind: "reorder",
        json_pointer: "/timelines/order",
        before: ["timeline:main"],
        after: ["timeline:main", "snapshot:timeline:main:1"],
      }),
      project,
    );
    expect(order.preview).toBe("版本记录已更新");
    expect(order.title).toBe("版本记录");
    expect(order.canInspect).toBe(false);
    expect(JSON.stringify([created, order])).not.toMatch(
      /snapshot:|elements_by_id|color_grade|timeline:main|\/timelines/u,
    );
  });

  it("does not describe a real live episode reorder as a version history update", () => {
    const result = fileReviewPresentation(
      makeReviewOperation({
        kind: "reorder",
        json_pointer: "/timelines/order",
        before: ["timeline:main", "timeline:second"],
        after: ["timeline:second", "timeline:main", "snapshot:timeline:main:1"],
      }),
      project,
    );
    expect(result).toMatchObject({
      title: "当前项目 · 顺序",
      preview: "顺序已调整",
      canInspect: false,
    });
  });

  it("does not stringify unknown fields, deleted structures, identifiers or provider errors", () => {
    for (const operation of [
      makeReviewOperation({
        json_pointer: "/assets/files_by_id/private",
        before: { relative_uri: "/tmp/private" },
        after: { checksum: "secret" },
      }),
      makeReviewOperation({
        kind: "delete",
        json_pointer: "/timelines/items/snapshot:private:1",
        before: { title: "备选剪辑", elements_by_id: { private: {} } },
        after: null,
      }),
      makeReviewOperation({
        json_pointer: "/description",
        before: "snapshot:private:1",
        after: '[RUNTIME_ACTION_RESULT] {"path":"/tmp/private"}',
      }),
    ]) {
      const result = fileReviewPresentation(operation, project);
      expect(result.hasTextDiff).toBe(false);
      expect(JSON.stringify(result)).not.toMatch(
        /\/tmp|snapshot:|checksum|relative_uri|elements_by_id|RUNTIME_ACTION/u,
      );
    }
  });

  it("resolves JSON pointer escaping and keeps public names from a deleted element", () => {
    const deleted = fileReviewPresentation(
      makeReviewOperation({
        kind: "delete",
        json_pointer:
          "/timelines/items/timeline:main/elements_by_id/seg~1opening",
        before: {
          label: "开场镜头",
          creation: { type: "edit" },
          span: { start_tick: 0, duration_tick: 10 },
        },
        after: null,
      }),
      project,
    );
    expect(deleted).toMatchObject({
      title: "剪辑片段 · 开场镜头",
      preview: "已删除剪辑片段",
      hasTextDiff: false,
    });
  });

  it("preserves authored prose and explicit duration values without exposing tick fields", () => {
    expect(
      fileReviewPresentation(
        makeReviewOperation({
          json_pointer: "/description",
          before: "A task-based story",
          after: "An ever-green forest",
        }),
      ).afterText,
    ).toBe("An ever-green forest");
    expect(
      fileReviewPresentation(
        makeReviewOperation({
          json_pointer: "/settings/target_duration_seconds",
          before: 3,
          after: 6,
        }),
      ),
    ).toMatchObject({
      title: "当前项目 · 时长",
      beforeText: "时长 3秒",
      afterText: "时长 6秒",
      hasTextDiff: true,
    });
  });
});
