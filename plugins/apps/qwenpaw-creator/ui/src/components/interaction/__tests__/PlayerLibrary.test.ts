import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const libraryHtml = readFileSync(
  resolve(process.cwd(), "../player/ivb/static/index.html"),
  "utf8",
);

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

async function openLibrary(status = 201) {
  vi.resetModules();
  document.body.innerHTML = libraryHtml;
  const project = {
    project_id: "night",
    title: "夜航",
    owner_user_id: "viewer",
    synopsis: "两条航线",
    node_count: 3,
    ending_count: 2,
    interaction_count: 1,
  };
  const fetchMock = vi.fn(async (_url: string, options: RequestInit) => {
    const upload = options.method === "POST";
    return {
      ok: !upload || status === 201,
      status: upload ? status : 200,
      text: async () =>
        JSON.stringify(
          upload
            ? status === 201
              ? project
              : { diagnostics: [{ message: "缺少视频分段" }] }
            : { projects: _url.includes("mine") ? [project] : [] },
        ),
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  await import("../../../../../player/ivb/static/state.js");
  await import("../../../../../player/ivb/static/app.js");
  return fetchMock;
}

it("uploads the selected bundle as multipart and shows it in the library", async () => {
  const fetchMock = await openLibrary();
  const file = new File(["zip bytes"], "night.zip", {
    type: "application/zip",
  });
  fireEvent.change(screen.getByLabelText("互动视频包"), {
    target: { files: [file] },
  });
  await screen.findByText("已导入《夜航》，点击作品开始观看。");
  expect(screen.getByRole("heading", { name: "夜航" })).toBeInTheDocument();
  const upload = fetchMock.mock.calls.find(
    ([, options]) => options.method === "POST",
  )!;
  expect(upload[0]).toMatch(/\/api\/projects$/);
  expect((upload[1].body as FormData).get("file")).toBe(file);
  // The browser supplies the multipart boundary; identity belongs to the gateway.
  expect(upload[1].headers).toEqual({});
});

it.each([
  [401, "请登录后再导入互动包。"],
  [422, "导入失败：缺少视频分段"],
])("keeps failed uploads retryable (%s)", async (status, message) => {
  await openLibrary(status as number);
  fireEvent.change(screen.getByLabelText("互动视频包"), {
    target: { files: [new File(["bad zip"], "bad.zip")] },
  });
  await screen.findByText(message);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "导入互动包" })).toBeEnabled(),
  );
  expect((screen.getByLabelText("互动视频包") as HTMLInputElement).value).toBe(
    "",
  );
});
