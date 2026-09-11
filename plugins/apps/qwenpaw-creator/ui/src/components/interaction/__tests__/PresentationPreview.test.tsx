import { render, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { projectDocument } from "@/test/creatorFixtures";
import { PresentationPreview } from "../PresentationPreview";

const runtime = (
  globalThis as typeof globalThis & {
    IVBAuthoredPlayer: { mount: (...args: any[]) => any };
  }
).IVBAuthoredPlayer;
afterEach(() => vi.restoreAllMocks());

it("keeps the preview mounted across identical snapshots and unrelated production updates", async () => {
  const handle = { dispose: vi.fn(), show: vi.fn(), inspect: vi.fn() };
  const mount = vi.spyOn(runtime, "mount").mockReturnValue(handle);
  const value = {
    ...structuredClone(projectDocument),
    description: "One light, two routes.",
    interactive_presentation: {
      design_prompt: "page design",
      screens: {},
      motion: {
        format: "html_css" as const,
        html: "<html><body>Agent page one</body></html>",
        fps: 24,
        loop: true,
        design_notes: "generated",
      },
    },
  };
  const view = render(<PresentationPreview project={value} />);
  await waitFor(() => expect(mount).toHaveBeenCalledTimes(1));
  view.rerender(
    <PresentationPreview
      project={{ ...structuredClone(value), generation: value.generation + 1 }}
    />,
  );
  await waitFor(() => expect(mount).toHaveBeenCalledTimes(1));
  expect(handle.dispose).not.toHaveBeenCalled();
  const adapter = mount.mock.calls[0][2];
  expect(adapter.segmentUrl("timeline:main")).toBe("");
  expect(adapter.reviewPoster).toMatch(/^data:image\/svg\+xml/);
  expect(mount.mock.calls[0][1].meta.synopsis).toBe("One light, two routes.");
  view.rerender(
    <PresentationPreview
      project={{ ...value, description: "Follow a light home." }}
    />,
  );
  await waitFor(() => expect(mount).toHaveBeenCalledTimes(2));
  expect(mount.mock.calls[1][1].meta.synopsis).toBe("Follow a light home.");
  view.rerender(
    <PresentationPreview
      project={{
        ...value,
        interactive_presentation: {
          ...value.interactive_presentation,
          motion: {
            ...value.interactive_presentation.motion,
            html: "<html><body>Agent page two</body></html>",
          },
        },
      }}
    />,
  );
  await waitFor(() => expect(mount).toHaveBeenCalledTimes(3));
  expect(handle.dispose).toHaveBeenCalledTimes(2);
});
