import { afterEach, describe, expect, it, vi } from "vitest";
import { installMockFetch } from "@/test/mockFetch";
import {
  IVB_UPLOAD_PATH,
  newUploadIdempotencyKey,
  uploadInteractiveBundle,
} from "@/api/creator/ivbUpload";

const UPLOAD_ROUTE = "qwenpaw/ivb/uploads";

function accepted(data: Record<string, unknown>) {
  return { request_id: "req-1", data };
}

function bundleBlob(size = 10) {
  return new Blob([new Uint8Array(size)], { type: "application/zip" });
}

describe("interactive bundle upload", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("posts the zip multipart to the platform with the console cookie", async () => {
    const { calls, fetchMock } = installMockFetch([
      {
        match: UPLOAD_ROUTE,
        response: {
          json: accepted({
            upload_id: "u-1",
            status: "received",
            progress_url: "https://platform/game?upload_id=u-1",
          }),
        },
      },
    ]);

    const result = await uploadInteractiveBundle({
      bundle: bundleBlob(),
      projectId: "project-abc",
      idempotencyKey: "key-1",
    });

    // Relative path: the endpoint only exists behind this deployment's
    // subdomain, and the browser carries the console cookie there.
    expect(IVB_UPLOAD_PATH).toBe("/api/v1/qwenpaw/ivb/uploads");
    expect(calls[0]).toMatchObject({ url: IVB_UPLOAD_PATH, method: "POST" });
    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      credentials: "include",
    });

    const body = calls[0].body as Record<string, unknown>;
    expect(body.project_id).toBe("project-abc");
    expect(body.file).toBeTruthy();

    // Idempotency-Key rides the header; Content-Type is deliberately left to
    // the browser so the multipart boundary survives. Headers are lowercased.
    expect(calls[0].headers["idempotency-key"]).toBe("key-1");
    expect(calls[0].headers["content-type"]).toBeUndefined();

    expect(result.upload_id).toBe("u-1");
    expect(result.progress_url).toContain("upload_id=u-1");
  });

  it("refuses an empty bundle before touching the network", async () => {
    const { calls } = installMockFetch([{ match: UPLOAD_ROUTE, response: {} }]);
    await expect(
      uploadInteractiveBundle({ bundle: new Blob(), projectId: "p" }),
    ).rejects.toThrow("为空");
    expect(calls).toHaveLength(0);
  });

  it("surfaces the platform status on a rejected upload", async () => {
    installMockFetch([
      {
        match: UPLOAD_ROUTE,
        response: { status: 400, ok: false, json: accepted({}) },
      },
    ]);
    await expect(
      uploadInteractiveBundle({ bundle: bundleBlob(), projectId: "p" }),
    ).rejects.toThrow("HTTP 400");
  });

  it("mints a fresh idempotency key each call", () => {
    expect(newUploadIdempotencyKey()).not.toBe(newUploadIdempotencyKey());
  });
});
