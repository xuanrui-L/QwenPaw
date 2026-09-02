import type { ProjectDocument, TimelineDocument } from "@/contracts/creator";
import type { ProjectEditOperation } from "@/store/projectSnapshotStore";

let _counter = 0;
function nextTimelineId(project: ProjectDocument): string {
  _counter += 1;
  const base = `timeline:${_counter}`;
  if (!project.timelines.items[base]) return base;
  return `timeline:${Date.now()}-${_counter}`;
}

export function createTimelineOperations(
  project: ProjectDocument,
  name: string,
): { timelineId: string; operations: ProjectEditOperation[] } {
  const timelineId = nextTimelineId(project);
  const timeline: TimelineDocument = {
    timeline_id: timelineId,
    name,
    description: "",
    ticks_per_second: 1000,
    color_grade: "",
    edit_plan: null,
    elements_by_id: {},
  };
  const orderIndex = project.timelines.order.length;
  return {
    timelineId,
    operations: [
      {
        op: "add",
        path: `/timelines/items/${timelineId}`,
        value: timeline,
        missingBefore: true,
      },
      {
        op: "add",
        path: `/timelines/order/${orderIndex}`,
        value: timelineId,
        missingBefore: true,
      },
    ],
  };
}

export function renameTimelineOperations(
  timelineId: string,
  name: string,
): ProjectEditOperation[] {
  return [
    {
      op: "replace",
      path: `/timelines/items/${timelineId}/name`,
      value: name,
    },
  ];
}

export function deleteTimelineOperations(
  project: ProjectDocument,
  timelineId: string,
): ProjectEditOperation[] {
  const idx = project.timelines.order.indexOf(timelineId);
  if (idx < 0) return [];
  return [
    {
      op: "remove",
      path: `/timelines/items/${timelineId}`,
      before: project.timelines.items[timelineId],
    },
    {
      op: "remove",
      path: `/timelines/order/${idx}`,
      before: project.timelines.order[idx],
    },
  ];
}

export function duplicateTimelineOperations(
  project: ProjectDocument,
  sourceId: string,
  name: string,
): { timelineId: string; operations: ProjectEditOperation[] } {
  const source = project.timelines.items[sourceId];
  if (!source) {
    return createTimelineOperations(project, name);
  }
  const timelineId = nextTimelineId(project);
  const copy: TimelineDocument = {
    ...source,
    timeline_id: timelineId,
    name,
    description: source.description,
    elements_by_id: {},
  };
  const orderIndex = project.timelines.order.length;
  return {
    timelineId,
    operations: [
      {
        op: "add",
        path: `/timelines/items/${timelineId}`,
        value: copy,
        missingBefore: true,
      },
      {
        op: "add",
        path: `/timelines/order/${orderIndex}`,
        value: timelineId,
        missingBefore: true,
      },
    ],
  };
}
