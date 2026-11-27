import { afterEach, expect, it, vi } from "vitest";
import { installMockFetch, type MockRoute } from "@/test/mockFetch";
import {
  FEEDBACK_LIMIT,
  makeFeedbackScreenshot,
  submitFeedback,
} from "../feedback";

const root = "/api/qwenpaw-creator/feedbacks";
const imageUrl = "https://cdn.example.org/shot.png";
const body = { projectId: "project-1", feedback: "切换镜头后预览卡住" };
const shot = () =>
  makeFeedbackScreenshot(new File(["shot"], "shot.png", { type: "image/png" }));

const routes = {
  "/draft": { json: { stage: "media_generation", trace_id: "trace-1" } },
  "/platform/account": { json: { id: "alice" } },
  "/platform/screenshots": { json: { url: imageUrl } },
  "/platform": { json: { submitted: true } },
};

function platform(
  overrides: Partial<Record<keyof typeof routes, MockRoute>> = {},
) {
  const responses = { ...routes, ...overrides };
  return installMockFetch(
    Object.entries(responses).map(([match, response]) => ({ match, response })),
  ).calls;
}

afterEach(() => vi.unstubAllGlobals());

it("sends the selected file separately from the user's description and diagnostics", async () => {
  const image = shot();
  const calls = platform();
  await submitFeedback(
    {
      ...body,
      sessionId: "s1",
      conversationId: "c1",
      contactEmail: " editor@example.com ",
      screenshots: [image],
    },
    {},
  );
  expect(calls.map((c) => c.url.slice(root.length))).toEqual([
    "/draft",
    "/platform/account",
    "/platform/screenshots",
    "/platform",
  ]);
  expect(calls[2].body).toEqual({ file: image.file });
  expect(calls[3].headers["x-platform-account"]).toBe("alice");
  const { description, screenshot_urls } = calls[3].body as {
    description: string;
    screenshot_urls: string[];
  };
  expect(description.split("\n\n")[0]).toBe(body.feedback);
  for (const field of [
    "editor@example.com",
    "project_id: project-1",
    "session_id: s1",
    "conversation_id: c1",
    "stage: media_generation",
    "trace_id: trace-1",
  ]) {
    expect(description).toContain(field);
  }
  expect(description).not.toContain(imageUrl);
  expect(screenshot_urls).toEqual([imageUrl]);
});

it("omits unavailable diagnostics and attachments for text-only feedback", async () => {
  vi.stubGlobal(
    "location",
    new URL("https://demo.qwenpaw.platform.agentscope.io"),
  );
  const calls = platform({ "/draft": { json: { stage: "unknown" } } });
  await submitFeedback(body, {});
  const payload = calls.at(-1)?.body as { description: string };
  expect(payload).not.toHaveProperty("screenshot_urls");
  expect(payload.description).toContain("deployment_id: demo");
  expect(payload.description).not.toMatch(
    /session_id|conversation_id|stage:|undefined/,
  );
});

it("retains uploads after a rejection and keeps retries on the original account", async () => {
  const feedback = { ...body, screenshots: [shot()] };
  const attempt = {};
  platform({
    "/platform": {
      ok: false,
      status: 400,
      json: { code: "feedback_rejected" },
    },
  });
  await expect(submitFeedback(feedback, attempt)).rejects.toMatchObject({
    code: "feedback_rejected",
  });
  const switched = platform({ "/platform/account": { json: { id: "bob" } } });
  await expect(submitFeedback(feedback, attempt)).rejects.toMatchObject({
    code: "FEEDBACK_ACCOUNT_CHANGED",
  });
  expect(switched.at(-1)?.url).toBe(root + "/platform/account");
  const retry = platform();
  await submitFeedback(feedback, attempt);
  expect(retry.map((c) => c.url)).not.toContain(root + "/platform/screenshots");
  const fresh = platform({ "/platform/account": { json: { id: "bob" } } });
  await submitFeedback(feedback, {});
  expect(
    fresh.find((c) => c.url.endsWith("/screenshots"))?.headers[
      "x-platform-account"
    ],
  ).toBe("bob");
});

it.each([
  [
    "/platform/account",
    { ok: false, status: 409, json: { message: "账号未连接" } },
    "账号未连接",
  ],
  ["/platform/screenshots", { json: {} }, "截图地址"],
] as const)("stops at %s on failure", async (path, response, error) => {
  const calls = platform({ [path]: response });
  await expect(
    submitFeedback({ ...body, screenshots: [shot()] }, {}),
  ).rejects.toThrow(error);
  expect(calls.at(-1)?.url).toBe(root + path);
});

it("checks the final description length before uploading", async () => {
  const calls = platform();
  await expect(
    submitFeedback(
      {
        ...body,
        feedback: "x".repeat(FEEDBACK_LIMIT),
        screenshots: [shot()],
      },
      {},
    ),
  ).rejects.toThrow("超出");
  expect(calls.map((c) => c.url)).toEqual([root + "/draft"]);
});

it.each([{ json: {} }, { ok: false, status: 502 }])(
  "does not replay a final POST with an unknown outcome",
  async (response) => {
    const calls = platform({ "/platform": response });
    const attempt = {};
    for (let click = 0; click < 2; click++) {
      await expect(submitFeedback(body, attempt)).rejects.toMatchObject({
        code: "FEEDBACK_UNCONFIRMED",
      });
    }
    expect(calls.filter((c) => c.url === root + "/platform")).toHaveLength(1);
  },
);
