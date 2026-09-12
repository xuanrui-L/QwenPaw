import { waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.replaceChildren();
});

it("refreshes imported works when restoring the library from browser history", async () => {
  document.body.innerHTML = `
    <div id="screen-library" hidden></div><div id="lib-grid"></div>
    <div id="lib-empty"></div><button id="lib-scope-all"></button>
    <button id="lib-scope-mine"></button><button id="lib-upload"></button>
    <input id="lib-upload-file" type="file"><div id="lib-upload-status"></div>
    <div id="fatal" hidden></div>`;
  const projects = vi
    .fn()
    .mockResolvedValueOnce({ projects: [] })
    .mockResolvedValueOnce({
      projects: [{ project_id: "real-story", title: "Imported story" }],
    });
  vi.stubGlobal("Api", { projects });
  await import("../../../../../player/ivb/static/app.js");
  await waitFor(() => expect(projects).toHaveBeenCalledWith("all"));
  expect(document.querySelector(".lib-card")).toBeNull();
  window.dispatchEvent(
    new PageTransitionEvent("pageshow", { persisted: true }),
  );
  await waitFor(() =>
    expect(document.querySelector(".lib-card-title")).toHaveTextContent(
      "Imported story",
    ),
  );
  expect(projects).toHaveBeenCalledTimes(2);
});
