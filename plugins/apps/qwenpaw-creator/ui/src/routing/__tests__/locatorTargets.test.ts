import { describe, expect, it } from "vitest";
import { resolveCreatorLocator } from "@/routing/locatorTargets";
import { projectDocument } from "@/test/creatorFixtures";

describe("durable review and object locator ownership", () => {
  it.each(["title", "synopsis", "description"])(
    "opens the actual blueprint for episode %s",
    (name) => {
      const field = `/timelines/items/timeline:ep2/${name}`;
      expect(resolveCreatorLocator({ page: "plan", field })).toEqual({
        page: "blueprint",
        field,
        timelineId: "timeline:ep2",
      });
    },
  );
  it.each([
    "description",
    "continuity",
    "variants/items/variant:default/prompt",
    "variants/items/variant:default/requirements",
  ])(
    "repairs a real generic Plan locator for visual %s without losing the original field",
    (suffix) => {
      const field = `/visual/entities/items/char:woman/${suffix}`;
      const input = { page: "plan", mediaType: "text", field };
      expect(resolveCreatorLocator(input)).toEqual({
        ...input,
        page: "assets",
        assetId: "char:woman",
        ...(suffix.startsWith("variants/")
          ? { variantId: "variant:default" }
          : {}),
      });
      expect(input.page).toBe("plan");
    },
  );

  it.each([
    "storyboard_prompt",
    "video_prompt",
    "storyboard_reference_version_ids",
    "video_reference_version_ids",
    "shots/items/s1_shot1/description",
  ])(
    "routes %s to the generation workbench for the exact owning timeline",
    (suffix) => {
      const field = `/timelines/items/timeline:ep2/elements_by_id/shot1_search/creation/${suffix}`;
      expect(
        resolveCreatorLocator({
          page: "plan",
          elementId: "shot1_search",
          field,
        }),
      ).toEqual({
        page: "element",
        timelineId: "timeline:ep2",
        elementId: "shot1_search",
        field,
      });
    },
  );

  it.each(["intent", "narrative"])(
    "keeps %s in the existing Plan detail",
    (suffix) => {
      const field = `/timelines/items/timeline:main/elements_by_id/shot1_search/creation/${suffix}`;
      expect(resolveCreatorLocator({ page: "plan", field })).toMatchObject({
        page: "plan",
        timelineId: "timeline:main",
        elementId: "shot1_search",
      });
    },
  );

  it("decodes JSON pointer tokens once and does not interpret malformed escaping", () => {
    const field =
      "/visual/entities/items/char~1woman/variants/items/variant~0night/prompt";
    expect(resolveCreatorLocator({ page: "plan" }, null, field)).toEqual({
      page: "assets",
      assetId: "char/woman",
      variantId: "variant~night",
    });
    expect(
      resolveCreatorLocator({ page: "plan" }, null, field.replace("~1", "~2")),
    ).toEqual({ page: "plan" });
  });

  it("resolves canonical entity@variant refs while leaving unrelated source ids opaque", () => {
    expect(
      resolveCreatorLocator(
        { page: "assets", assetId: "cat@variant:cat:default" },
        projectDocument,
      ),
    ).toEqual({
      page: "assets",
      assetId: "cat",
      variantId: "variant:cat:default",
    });
    expect(
      resolveCreatorLocator(
        { page: "assets", assetId: "source@version" },
        projectDocument,
      ),
    ).toEqual({ page: "assets", assetId: "source@version" });
  });
});
