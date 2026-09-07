import { describe, expect, it } from "vitest";
import type { ProjectDocument } from "@/contracts/creator";
import { presentPromptEntityNames } from "../promptEntityNames";

const project = {
  visual: {
    entities: {
      items: {
        "char:woman": { name: "找钥匙的女子" },
        "scene:apartment_door": { name: "公寓门口" },
        "prop:silver_key": { name: "银色钥匙" },
      },
    },
  },
} as unknown as ProjectDocument;

describe("prompt entity names", () => {
  it("names the actual entities in storyboard prose and video image citations", () => {
    const value =
      "角色身份锁定：char:woman（灰色外套）。\n[Image 2]为角色锚点（char:woman），[Image 3]为场景（scene:apartment_door），[Image 4]为道具（prop:silver_key）。";
    expect(presentPromptEntityNames(value, project)).toBe(
      "角色身份锁定：找钥匙的女子（灰色外套）。\n[Image 2]为角色锚点（找钥匙的女子），[Image 3]为场景（公寓门口），[Image 4]为道具（银色钥匙）。",
    );
    expect(presentPromptEntityNames("visual-entity:char:woman", project)).toBe(
      "找钥匙的女子",
    );
  });

  it("preserves literal dialogue, titles, URLs, code and unknown or compound ids", () => {
    const literal = [
      '她说“char:woman”，字幕为"scene:apartment_door"。',
      "台词：char:woman",
      "《char:woman》 'prop:silver_key' `char:woman`",
      "https://example.com/char:woman?q=prop:silver_key",
      "oss://bucket/ref?id=char:woman 「char:woman」 『scene:apartment_door』",
      String.raw`"say \"char:woman\" please"`,
      '"char:woman\nscene:apartment_door"',
      "char:unknown char:woman_extra char:woman@variant:default /char:woman asset:char:woman",
      "````",
      "```",
      "char:woman",
      "````",
      "~~~",
      "char:woman",
      "~~~",
      "    char:woman",
    ].join("\n");
    expect(presentPromptEntityNames(literal, project)).toBe(literal);
    expect(presentPromptEntityNames("角色 char:woman", null)).toBe(
      "角色 char:woman",
    );
  });

  it("does not recursively interpret a public name or change ordinary word ids", () => {
    const named = {
      visual: {
        entities: {
          items: {
            "char:woman": { name: "prop:silver_key" },
            "prop:silver_key": { name: "钥匙" },
            woman: { name: "女子" },
          },
        },
      },
    } as unknown as ProjectDocument;
    expect(presentPromptEntityNames("char:woman woman", named)).toBe(
      "char:woman woman",
    );
  });
});
