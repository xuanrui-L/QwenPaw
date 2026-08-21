# -*- coding: utf-8 -*-
"""Teach live operation the way the host does: reuse its own browser skill.

The authoritative API surface belongs to the main repository, so it is loaded
from the installed browser skill verbatim rather than restated here — a copy
would drift, and a drifted manual teaches a closed API that no longer exists.
Creator only appends what is genuinely its own: how recording works, and where
the resulting footage ends up.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILL_CANDIDATES = ("browser-zh", "browser-en")
_FRONT_MATTER_FENCE = "---"

# Where the host computer-use bundle keeps its own authoritative skill. It is
# only importable at runtime, so the manual is read from disk by best-effort
# path discovery rather than a static import.
_COMPUTER_USE_SKILL_RELATIVE = (
    "plugins/bundle/computer-use/skills/computer_use/SKILL.md"
)

# Creator's own contract on top of the host SDK. Recording bounds are the only
# thing the model must decide deliberately, so that is what this states — with
# no prescribed order of work, because the flow is the model's to design.
_CREATOR_SECTION = """
## Creator 扩展：录制与产物（`recorder`）

`browser_use` 的作用域里除 `Browser` 外还有 `recorder`：

- `await recorder.start(label="这段在做什么") -> take_id`：开始录制当前操作的页面。
- `await recorder.stop() -> {take_id, label, summary}`：结束这一段并落地成片段。
- `recorder.is_recording() -> bool`：当前是否正在录制。

只有 `start` 与 `stop` 之间的画面会进入录像。感知、思考、试错、等待都发生在录制
区间之外，因此片段里不会有废镜头——这也是让后续读片和剪辑成本保持低的原因。

**是否需要录屏由你判断**：真实的动态过程（连续操作、页面跳转、滚动浏览）值得录；
只是要展示某个静态界面时，`await page.screenshot()` 配合动效通常更省也更清楚。
需要重录时，重新操作一遍再录即可。

产物会自动成为 Project 源素材，与用户上传的素材完全同构：
- 录像片段 → 源素材（可用 `observe_source_clip` 看片、可作为 Edit Element 的
  `render_source` 进入时间轴）；
- 截图 → 项目图片素材（可上轨、可作参考图）；
- 工具返回值里给出每个片段与截图的 `workspaceRef` / `sourceAssetVersionId`，
  后续委派与编排直接引用这些 ID。

录制期间，每个操作的坐标与时刻会被自动记录在该片段的事实清单里（你无需做任何
额外的事），动效制作会用它把强调放在操作真正发生的位置上。

### 本后端已验证的注意事项

- 用 `await locator.scroll()` 滚动；坐标滚轮 `page.mouse.wheel(...)` 在当前后端
  不受支持，会直接失败。
- 定位失败时先 `await page.snapshot()` 看真实的 role 与可访问名，不要凭猜测重试
  同一个定位器。
- 登录、验证码、2FA 一律 `await browser.handoff(...)` 后停止，绝不自动化。
"""


# Creator's desktop recording contract on top of the host computer-use runtime.
_COMPUTER_USE_SECTION = """
## Creator 扩展：桌面录制与产物（`desktop` / `recorder`）

`computer_use` 的作用域里有 `desktop`（observe_window / list_windows /
list_apps / launch_app / click / type_text / press_key / scroll / drag /
invoke_element / set_value / close_window）与 `recorder`：

- `await recorder.start(label="这段在做什么") -> take_id`：开始录制当前窗口。
- `await recorder.stop() -> {take_id, label, summary}`：结束这一段。
- `recorder.is_recording() -> bool`。

先 `await desktop.observe_window()` 拿到窗口与元素，再动作，再 observe
确认。录制会按当前窗口边界裁剪屏幕；只有 start–stop 之间的画面进入
录像，因此片段里没有废镜头。是否录屏、录哪几段由你判断；只展示静态
界面时，截图配动效往往更好。

产物与浏览器完全同构：录像片段→源素材（可 observe_source_clip 看片、可作
 Edit 的 render_source）；截图→图片素材；工具返回值给出每个片段与截图的
 workspaceRef / sourceAssetVersionId。录制期间每个动作的坐标与时刻会自动记入
该片段的事实清单，动效制作用它把强调放在操作真正发生的位置上。

### 注意事项

- 桌面操作需要桌面宿主运行时（仅 Windows / macOS）；无头服务上不可用，
  工具会返回明确的降级提示。
- 每个动作只根据最新的 observe 结果决定；`dispatched: true` 只说明输入已发
  送，不代表应用完成。
"""


def _skill_root() -> Path | None:
    try:
        import qwenpaw
    except ImportError:  # pragma: no cover - Creator always runs inside host
        return None
    module_file = getattr(qwenpaw, "__file__", None)
    if not module_file:
        return None
    return Path(module_file).resolve().parent / "agents" / "skills"


def _strip_front_matter(text: str) -> str:
    """Drop the skill's YAML header, which is loader metadata, not guidance."""
    stripped = text.lstrip()
    if not stripped.startswith(_FRONT_MATTER_FENCE):
        return text.strip()
    closing = stripped.find(
        f"\n{_FRONT_MATTER_FENCE}",
        len(_FRONT_MATTER_FENCE),
    )
    if closing == -1:
        return stripped
    remainder = stripped[closing + len(_FRONT_MATTER_FENCE) + 1 :]
    return remainder.strip()


def load_host_browser_manual() -> str:
    """Return the host's browser skill body, or an empty string if absent."""
    root = _skill_root()
    if root is None:
        return ""
    for name in _SKILL_CANDIDATES:
        candidate = root / name / "SKILL.md"
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        body = _strip_front_matter(text)
        if body:
            return body
    logger.info(
        "host browser skill unavailable; live guidance is minimal",
    )
    return ""


def load_host_computer_use_manual() -> str:
    """Return the host computer-use skill body, or empty when not found."""
    root = _skill_root()
    # The bundle sits three levels up from the installed qwenpaw package
    # (repo/plugins/bundle/...). Walk up from the package to find it, so the
    # authoritative manual is reused verbatim rather than restated.
    candidates: list[Path] = []
    if root is not None:
        repo = root.parent.parent.parent.parent
        candidates.append(repo / _COMPUTER_USE_SKILL_RELATIVE)
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        body = _strip_front_matter(text)
        if body:
            return body
    return ""


def live_operation_guidance() -> str:
    """Render the browser guidance injected into the Creator system prompt.

    Empty when the capability is switched off, so a deployment without live
    operation never pays for guidance about a tool the model cannot call.
    """
    from models.config import get_live_operation_enabled

    if not get_live_operation_enabled():
        return ""
    manual = load_host_browser_manual()
    header = (
        "# 真实网站操作（`browser_use`）\n\n"
        "`browser_use` 让你用异步 Python 真实操作网站：驱动的是 QwenPaw 自己的 "
        "Browser SDK，不是 Playwright，未列出的方法不存在。下面是该 SDK 的完整"
        "参考，以及 Creator 侧的录制约定。流程怎么组织由你决定。"
    )
    parts = [header]
    if manual:
        parts.append(manual)
    parts.append(_CREATOR_SECTION.strip())
    parts.append(_computer_use_guidance())
    return "\n\n".join(part for part in parts if part)


def _computer_use_guidance() -> str:
    """Desktop guidance, injected only when the desktop tool is callable."""
    from models.config import get_computer_use_enabled

    if not get_computer_use_enabled():
        return ""
    manual = load_host_computer_use_manual()
    header = (
        "# 真实桌面操作（`computer_use`）\n\n"
        "`computer_use` 让你用异步 Python 真实操作桌面应用（复用宿主 "
        "Computer Use 原生运行时）。下面是它的完整参考，以及 Creator 侧的"
        "录制约定。流程怎么组织由你决定。"
    )
    parts = [header]
    if manual:
        parts.append(manual)
    parts.append(_COMPUTER_USE_SECTION.strip())
    return "\n\n".join(parts)


__all__ = [
    "live_operation_guidance",
    "load_host_browser_manual",
    "load_host_computer_use_manual",
]
