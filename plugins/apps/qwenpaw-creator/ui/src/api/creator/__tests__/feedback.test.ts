import { afterEach, describe, expect, it, vi } from "vitest";
import { installMockFetch } from "@/test/mockFetch";
import {
  CREATOR_FEEDBACK_PATH,
  FEEDBACK_LIMIT,
  fetchTracePointer,
  submitCreatorFeedback,
} from "@/api/creator/feedback";

const FEEDBACK_ROUTE = "creator/feedbacks";
const TRACES_ROUTE = "observability/traces";

const DRAFT = {
  project_id: "project-c0d79742eca95833a25d7db621abe73e",
  stage: "media_generation",
  feedback: "镜头生成失败只提示生成失败，看不出是模型报错还是 Credits 不够。",
};

function accepted(overrides: Record<string, unknown> = {}) {
  return {
    request_id: "req-1",
    data: {
      id: "fb-uuid-1",
      user_id: "u-1",
      deployment_id: "d-1",
      created_at: "2026-09-17T10:24:31",
      ...overrides,
    },
  };
}

describe("creator feedback submission", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts to the platform on a relative path with the console cookie", async () => {
    const { calls, fetchMock } = installMockFetch([
      { match: FEEDBACK_ROUTE, response: { json: accepted() } },
    ]);

    const record = await submitCreatorFeedback(DRAFT);

    expect(record.id).toBe("fb-uuid-1");
    // Relative because the endpoint only exists behind this deployment's
    // subdomain; an absolute platform URL would carry no console_token.
    expect(CREATOR_FEEDBACK_PATH).toBe("/api/v1/qwenpaw/creator/feedbacks");
    expect(calls[0]).toMatchObject({
      url: CREATOR_FEEDBACK_PATH,
      method: "POST",
    });
    // The platform's only credential is the cookie, and it ignores the
    // bearer Creator's own client would add.
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      credentials: "include",
    });
  });

  it("sends the three required fields and no identity of its own", async () => {
    const { calls } = installMockFetch([
      { match: FEEDBACK_ROUTE, response: { json: accepted() } },
    ]);

    await submitCreatorFeedback({ ...DRAFT, trace_line: 137 });

    expect(calls[0].body).toEqual({
      project_id: DRAFT.project_id,
      stage: "media_generation",
      feedback: DRAFT.feedback,
      trace_line: 137,
    });
    // user_id belongs to the body of a broken contract: the server writes the
    // identity it authenticated from the cookie and trusts nothing else.
    expect(calls[0].body).not.toHaveProperty("user_id");
  });

  it("drops an absent trace pointer instead of sending empty strings", async () => {
    const { calls } = installMockFetch([
      { match: FEEDBACK_ROUTE, response: { json: accepted() } },
    ]);

    await submitCreatorFeedback({
      ...DRAFT,
      trace_id: "",
      trace_file: "creator-trace-2026-09-17.jsonl",
    });

    expect(calls[0].body).not.toHaveProperty("trace_id");
    expect(calls[0].body).toHaveProperty(
      "trace_file",
      "creator-trace-2026-09-17.jsonl",
    );
  });

  it("refuses locally before spending a request on an empty or oversized body", async () => {
    const { calls } = installMockFetch([
      { match: FEEDBACK_ROUTE, response: { json: accepted() } },
    ]);

    await expect(
      submitCreatorFeedback({ ...DRAFT, stage: "  " }),
    ).rejects.toThrow("阶段");
    await expect(
      submitCreatorFeedback({
        ...DRAFT,
        feedback: "字".repeat(FEEDBACK_LIMIT + 1),
      }),
    ).rejects.toThrow(String(FEEDBACK_LIMIT));
    expect(calls).toHaveLength(0);
  });

  it("names the cookie when the platform does not recognise the session", async () => {
    installMockFetch([
      { match: FEEDBACK_ROUTE, response: { status: 401, ok: false } },
    ]);

    await expect(submitCreatorFeedback(DRAFT)).rejects.toThrow("console_token");
  });
});

describe("trace pointer lookup", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("maps the newest record onto the platform's field names", async () => {
    installMockFetch([
      {
        match: TRACES_ROUTE,
        response: {
          json: {
            // Ascending by timestamp: the last row is the one to cite.
            items: [
              {
                traceId: "trace-old",
                traceFile: "creator-trace-2026-09-16.jsonl",
                traceLine: 12,
                timestamp: "2026-09-16T09:45:21.642273+00:00",
              },
              {
                traceId: "trace-new",
                traceFile: "creator-trace-2026-09-17.jsonl",
                traceLine: 137,
                timestamp: "2026-09-17T02:45:06.871221+00:00",
              },
            ],
          },
        },
      },
    ]);

    expect(await fetchTracePointer({ projectId: DRAFT.project_id })).toEqual({
      trace_id: "trace-new",
      trace_file: "creator-trace-2026-09-17.jsonl",
      trace_line: 137,
      trace_ts: "2026-09-17T02:45:06.871221+00:00",
    });
  });

  it("keeps the feedback when observability is switched off", async () => {
    installMockFetch([
      { match: TRACES_ROUTE, response: { status: 503, ok: false } },
    ]);

    expect(await fetchTracePointer({ projectId: DRAFT.project_id })).toEqual(
      {},
    );
  });
});
