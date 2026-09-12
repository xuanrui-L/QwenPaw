# -*- coding: utf-8 -*-
"""Structural interface contract only; no visual templates or default styles.

Mirrored in the standalone player. The host binds declared actions and data;
the authored document owns every screen's markup, CSS and animation.
"""

from collections import Counter

from .interaction_html import _InteractionParser

PRESENTATION_ACTIONS = {
    "title": {"start", "resume", "map", "replay", "reset"},
    "play": {"toggle_play", "map", "replay", "title"},
    "map": {"map_back", "jump", "title", "reset"},
    "ending": {"replay", "title", "map", "reset"},
}
PRESENTATION_REQUIRED_ACTIONS = {
    ("title", "start"),
    ("title", "resume"),
    ("title", "map"),
    ("title", "replay"),
    ("play", "map"),
    ("play", "toggle_play"),
    ("play", "replay"),
    ("map", "map_back"),
    ("ending", "replay"),
    ("ending", "title"),
}


class _PresentationParser(_InteractionParser):
    def __init__(self):
        super().__init__()
        self.screens = []
        self.actions = []
        self.nodes = []
        self.video = 0
        self.slots = 0
        self.stack = []
        self.scoped_actions = []
        self.roots = Counter()
        self.button = None
        self.labels = {}
        self.bindings = set()

    def handle_starttag(self, tag, attrs):
        # Each independent protocol rule contributes a diagnostic.
        # pylint: disable=too-many-branches
        # Video is a host-owned media surface. URLs may only be assigned by
        # the trusted controller; the base parser still rejects all src/on*.
        super().handle_starttag("div" if tag == "video" else tag, attrs)
        values = dict(attrs)
        if tag in {"html", "head", "body"}:
            self.roots[tag] += 1
        screen = values.get("data-screen")
        parent_screen = next(
            (s for _, s, _ in reversed(self.stack) if s),
            None,
        )
        if screen:
            self.screens.append(screen)
            if parent_screen:
                self.problems.append("screens must not be nested")
        scope = screen or parent_screen
        binding = values.get("data-bind")
        if binding:
            self.bindings.add((scope, binding))
            if self.button is not None or screen:
                self.problems.append("data-bind requires a text-only node")
        if tag in {"button", "video"} or "data-slot" in values:
            if binding or any(b for _, _, b in self.stack):
                self.problems.append("data-bind must not contain controls")
        action = values.get("data-action")
        if action:
            self.actions.append(action)
            self.scoped_actions.append((scope, action))
            if tag != "button" or any(
                attr in values
                for attr in ("disabled", "hidden", "data-host-hidden")
            ):
                self.problems.append("data-action requires an enabled button")
            if action not in PRESENTATION_ACTIONS.get(scope, set()):
                self.problems.append("unsupported action for screen")
            if self.button is not None:
                self.problems.append("nested action buttons")
            self.button = (scope, action, [])
            if action == "jump" and not values.get("data-node-ref"):
                self.problems.append("jump requires data-node-ref")
        if "data-node-ref" in values:
            self.nodes.append(values["data-node-ref"])
        if tag == "video":
            self.video += 1
            if "data-player-video" not in values or scope != "play":
                self.problems.append(
                    "video requires data-player-video in play",
                )
            if any(a in values for a in ("autoplay", "loop", "poster")):
                self.problems.append("host owns video playback and sources")
        if values.get("data-slot") == "interaction":
            self.slots += 1
            if scope != "play":
                self.problems.append("interaction slot must be in play")
        if tag not in {"br", "hr", "meta"}:
            self.stack.append((tag, screen, binding))

    def handle_endtag(self, tag):
        super().handle_endtag(tag)
        if tag == "button" and self.button is not None:
            scope, action, chunks = self.button
            self.labels.setdefault((scope, action), []).append(
                " ".join("".join(chunks).split()),
            )
            self.button = None
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        elif tag not in {"br", "hr", "meta"}:
            self.problems.append(f"unbalanced HTML tag: {tag}")

    def handle_data(self, data):
        super().handle_data(data)
        if self.button is not None:
            self.button[2].append(data)

    def validate_homepage_labels(self):
        for action in ("start", "map", "replay"):
            labels = self.labels.get(("title", action), [])
            if not any(
                any(char.isalnum() for char in label) for label in labels
            ):
                self.problems.append(
                    f"homepage {action} requires visible button text",
                )

    def validate_text_bindings(self):
        required_bindings = {
            ("title", "project.title"),
            ("title", "project.synopsis"),
            ("ending", "node.title"),
            ("ending", "node.synopsis"),
        }
        if not required_bindings.issubset(self.bindings):
            self.problems.append(
                "missing required screen text bindings: "
                + str(sorted(required_bindings - self.bindings)),
            )


def validate_presentation_html(
    html: str,
    node_ids=None,
    screens=None,
) -> list[str]:
    parser = _PresentationParser()
    if not 32 <= len(html) <= 200_000:
        parser.problems.append("HTML length must be between 32 and 200000")
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        parser.problems.append("malformed presentation HTML")
    if parser.stack:
        parser.problems.append("unclosed presentation HTML tags")
    if parser.roots != Counter({"html": 1, "head": 1, "body": 1}):
        parser.problems.append("one html/head/body required")
    if Counter(parser.screens) != Counter(["title", "play", "map", "ending"]):
        parser.problems.append(
            "exactly one title/play/map/ending screen required",
        )
    if parser.video != 1 or parser.slots != 1:
        parser.problems.append("one video and one interaction slot required")
    parser.validate_text_bindings()
    parser.validate_homepage_labels()
    required = PRESENTATION_REQUIRED_ACTIONS
    if not required.issubset(set(parser.scoped_actions)):
        parser.problems.append(
            "missing required screen actions: "
            + str(sorted(required - set(parser.scoped_actions))),
        )
    allowed = {
        "start",
        "resume",
        "map",
        "map_back",
        "toggle_play",
        "replay",
        "title",
        "jump",
        "reset",
    }
    if set(parser.actions) - allowed:
        parser.problems.append("unknown data-action")
    if node_ids is not None and set(parser.nodes) != set(node_ids):
        parser.problems.append(
            "data-node-ref must cover exactly the live story nodes",
        )
    for screen, design in (screens or {}).items():
        for action, control in design.get("controls", {}).items():
            labels = parser.labels.get((screen, action), [])
            label = " ".join(control.get("label", "").split())
            if not labels or (label and any(v != label for v in labels)):
                parser.problems.append(
                    f"control {screen}.{action} must honor its declared label",
                )
    return list(dict.fromkeys(parser.problems))
