import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { ConfigProvider } from "antd";
import { describe, expect, it, vi } from "vitest";
import ShotList from "../ShotList";
import type {
  ProjectEntityCollection,
  ShotDocument,
} from "@/contracts/creator";
import { editShotContent, shotContentText } from "@/lib/shotContent";
import i18n from "@/i18n";
import { findCreatorFieldElement } from "@/routing/reviewFocus";

vi.mock("@/components/agent/InlineReviewDiff", () => ({ default: () => null }));

const initial: ShotDocument = {
  shot_id: "first",
  description: "人物看向窗外。旁白：雨终于停了。只有雨滴声，无配乐。",
  dialogue: "我们走吧。",
  camera: "固定",
  framing: "中景",
  duration_seconds: 6,
};

function Editor({ onEdit }: { onEdit: (value: ShotDocument) => void }) {
  const [shots, setShots] = useState<ProjectEntityCollection<ShotDocument>>({
    order: [initial.shot_id],
    items: { [initial.shot_id]: { ...initial } },
  });
  return (
    <ConfigProvider theme={{ token: { motion: false } }}>
      <ShotList
        shots={shots}
        elementId="clip"
        shotPointer={(id, field) => `/shots/items/${id}/${field}`}
        onAdd={() => undefined}
        onDelete={() => undefined}
        onChangeField={(id, field, value) => {
          const updated = { ...shots.items[id] };
          if (field === "description") editShotContent(updated, String(value));
          else Object.assign(updated, { [field]: value });
          setShots({ order: shots.order, items: { [id]: updated } });
          onEdit(updated);
        }}
      />
    </ConfigProvider>
  );
}

describe("unified shot content", () => {
  it("keeps legacy dialogue and narration visible in a single editable description", () => {
    render(<Editor onEdit={vi.fn()} />);
    const editor = screen.getByRole("textbox", {
      name: i18n.t("r2v.plan.actionAria", { index: 1 }),
    });
    expect(editor).toHaveValue(
      `${initial.description}\n\n${i18n.t(
        "r2v.plan.dialogueLabel",
      )}：我们走吧。`,
    );
    expect(document.querySelectorAll(".r2v-plan-shot textarea")).toHaveLength(
      1,
    );
    expect(document.querySelectorAll(".r2v-plan-dialogue")).toHaveLength(0);
    expect(
      findCreatorFieldElement("/shots/items/first/dialogue")?.querySelector(
        "textarea",
      ),
    ).toBe(editor);
  });

  it("removes obsolete hidden speech when the user revises the complete content", () => {
    const onEdit = vi.fn();
    render(<Editor onEdit={onEdit} />);
    const revised =
      "人物看向窗外，随后离开。全段有意静默，无对白、旁白或配乐，仅保留雨滴声。";
    fireEvent.change(
      screen.getByRole("textbox", {
        name: i18n.t("r2v.plan.actionAria", { index: 1 }),
      }),
      { target: { value: revised } },
    );
    expect(onEdit).toHaveBeenLastCalledWith({
      ...initial,
      description: revised,
      dialogue: "",
    });
    expect(
      screen.getByRole("textbox", {
        name: i18n.t("r2v.plan.actionAria", { index: 1 }),
      }),
    ).toHaveValue(revised);
    expect(screen.queryByDisplayValue("我们走吧。")).not.toBeInTheDocument();
  });

  it("does not duplicate spoken lines already included in the complete description", () => {
    const description = "人物说：‘我们走吧。’ 画外旁白：‘雨停了。’";
    expect(shotContentText({ description, dialogue: "我们走吧。" })).toBe(
      description,
    );
  });
});
