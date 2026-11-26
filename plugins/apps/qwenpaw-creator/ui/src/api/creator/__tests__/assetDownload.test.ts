import { afterEach, expect, it, vi } from "vitest";
import { installMockFetch } from "@/test/mockFetch";
import { generateAssetDownload, type DownloadAsset } from "../assetDownload";

const signed = {
  type: "video",
  name: "片段 A+B.mp4",
  size: 4321,
  expires_in: 86400,
  preview_url: "https://media.example/cut.mp4?signature=A%2BB",
  download_url:
    "https://files.example/cut.mp4?signature=C%3D&disposition=attachment",
};
const signPath = "/api/v1/qwenpaw/creator/assets/oss-urls";
const asset = { kind: "artifact", versionId: "shot:v1", name: "片段" } as const;
const reply = (match: string, json: unknown) => ({ match, response: { json } });
const lookup = reply("/projects/p/media/artifact/shot%3Av1/oss-source", {
  relativePath: "p/assets/cut.mp4",
});
const sign = (asset: DownloadAsset) =>
  generateAssetDownload("p", asset, new AbortController().signal);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it.each([3600, 86400])(
  "preserves both signed URLs and the platform lifetime of %i seconds",
  async (expires_in) => {
    vi.spyOn(Date, "now").mockReturnValue(1000);
    const { calls, fetchMock } = installMockFetch([
      lookup,
      reply(signPath, { data: { ...signed, expires_in } }),
    ]);
    const link = await sign(asset);
    expect(calls[1]).toMatchObject({
      url: signPath,
      method: "POST",
      body: { project_id: "p", relative_path: "p/assets/cut.mp4" },
    });
    expect(fetchMock.mock.calls[1][1]?.credentials).toBe("include");
    expect(calls[1].headers.authorization).toBeUndefined();
    const page = new URL(link.url);
    expect(page.origin + page.pathname + page.search).toBe(
      "https://platform-oss.agentscope.io/qwenpaw-creator/download/index.html",
    );
    expect(Object.fromEntries(new URLSearchParams(page.hash.slice(1)))).toEqual(
      {
        type: signed.type,
        name: signed.name,
        size: String(signed.size),
        src: signed.preview_url,
        url: signed.download_url,
      },
    );
    expect(link.directUrl).toBe(signed.download_url);
    expect(link.expiresAt).toBe(1000 + expires_in * 1000);
  },
);

it("signs the assembled file in the requested episode order", async () => {
  const relativePath = "p/runtime/whole-films/revision/full-film.mp4";
  const versionIds = ["ep2", "ep1"];
  const { calls } = installMockFetch([
    reply("/projects/p/whole-film", {
      status: "ready",
      relativePath,
      sizeBytes: 4321,
      key: "revision",
    }),
    reply(signPath, { data: signed }),
  ]);
  await sign({ kind: "whole-film", versionIds, name: "整片" });
  expect(calls.map(({ body }) => body)).toEqual([
    { versionIds },
    { project_id: "p", relative_path: relativePath },
  ]);
});
