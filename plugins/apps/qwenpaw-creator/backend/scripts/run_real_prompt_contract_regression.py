"""Real qwen-image3 → HappyHorse prompt-contract regression.

The script reuses approved local identity assets, renders a corrected
three-panel storyboard, then submits a legal three-second HappyHorse R2V job
with the exact stable reference order.  It never persists or prints API keys.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


STORYBOARD_PROMPT = """\
Production-ready clean cinematic generation-reference storyboard, 16:9 outer
delivery canvas, exactly 3 illustrated panels read left-to-right then top-to-
bottom. One compact 3-second story: inside the same rain-dark lighthouse core,
small worn robot Amu raises one three-fingered hand toward the dormant optical
lens; the cyan origami crane hovers beside the hand; a single cyan energy pulse
travels from Amu's chest light into the lens; the lens changes from dark to warm
gold and Amu remains alone beside the restored crane. These are three action
phases of one continuous dominant action, not three equal-duration edits:
preparation → decisive energy transfer → clear illuminated end state.

Reference responsibilities, in supplied order: the first reference is the only
identity source for Amu's rounded head, compact three-head-tall body, matte
off-white worn shell, dark seams, short three-fingered hands, visible old joints
and single cyan circular chest light. The second is the only identity source for
the one-palm-size, single-sheet cyan translucent origami crane with sharp classic
folds. The third supplies the same cold-blue rainy lighthouse world, round stone
interior, rusted spiral stair and central optical lens. Do not inherit any
identity-board white background, repeated views, atlas divisions, arrows,
labels or diagrams.

IDENTITY AND COUNT LOCK: the same single Amu appears once in each illustrated
panel. Exactly one Amu per panel and exactly one origami crane per panel. Never
clone, duplicate, twin, multi-expose or add a second robot or crane. Preserve
the same face, proportions, shell wear, joints, hand design and chest-light
placement in all panels. No people or other creatures.

PANEL 1 — preparation: medium-wide low three-quarter rear view inside the dark
lighthouse core. Amu stands screen-left facing the dormant lens at screen-right,
one hand beginning to lift; the crane hovers between hand and lens. Cold-blue
rain spill enters from the doorway, lens completely dark. End state: open palm
aimed at lens, chest light cyan and still on.

PANEL 2 — decisive transfer: medium close side view. A single narrow cyan energy
arc connects Amu's one chest light through the raised palm to the optical lens;
the crane holds position just above the hand. The robot braces its weight but
does not split or change design. Lens begins glowing at its center. End state:
energy contact established, warm-gold ignition beginning.

PANEL 3 — illuminated end state: wide shot from behind Amu. The lens is now one
stable warm-gold light source, revealing the spiral iron stair and wet stone;
Amu lowers the same hand, chest light dim but present, and the same single crane
glows cyan at shoulder height. End state is calm and readable, ready for a cut.

Unified high-end cinematic 3D animation concept-frame rendering, cold cyan and
deep navy shadows changing to warm gold only in the final panel, clear action
silhouettes, consistent rain atmosphere. No title, header, footer, panel number,
caption, label, legend, timestamp, UI, logo, watermark, arrow or annotation.
"""


VIDEO_PROMPT = """\
Create one continuous 3-second premium cinematic 3D animation shot in 16:9.
One dominant action only: the single small worn robot Amu transfers one cyan
energy pulse from its chest light through its raised hand into the dormant
lighthouse lens; the lens ignites warm gold; Amu lowers the hand beside the
single restored cyan origami crane. No dialogue.

[Image 1] is the three-panel storyboard. Adopt only its reading order,
approximate composition, preparation-to-transfer-to-illumination action chain
and camera rhythm. Do not render its 2×2 lattice, blank slot, gutters, borders,
panel divisions or storyboard layout into the video.

[Image 2] is Amu's character identity. Preserve its one rounded head, compact
three-head-tall proportions, matte off-white worn shell, dark seams, short
three-fingered hands, old visible joints and one cyan circular chest light. Do
not inherit the white identity-board background, repeated views, labels,
silhouettes or detail tiles.

[Image 3] is the origami crane prop identity. Preserve one palm-size cyan
translucent crane, a single sheet with sharp classic folds and soft cyan glow.
Do not inherit multiple study copies, scale diagrams, white background or
detail tiles.

[Image 4] is the scene identity. Preserve the rain-dark round stone lighthouse
core, rusted spiral stair, wet materials, dormant optical lens and cold-blue
ambient palette. Do not inherit atlas divisions, topology arrows or layout.

Opening beat: medium three-quarter rear view, Amu alone at screen-left faces the
dark lens at screen-right and smoothly raises one open hand; the crane holds
beside the palm. Dominant action: one cyan pulse leaves the chest light, travels
through the hand and makes continuous contact with the lens while the camera
makes one restrained push-in; the robot braces without changing proportions.
Final beat: the lens blooms once into stable warm gold, revealing wet stone and
the spiral stair; Amu lowers the same hand, chest light remains present but dim,
and the same crane glows at shoulder height. Hold the clear end state briefly.

Identity/count invariants: exactly one Amu and exactly one crane for the entire
video; no clone, twin, extra robot, extra crane, multi-exposure, morphing,
missing limbs or added character. Keep one continuous lighthouse space and one
continuous camera move. No cuts to identity boards or scene atlas. No grid,
border, text, caption, UI, logo or watermark. Sound: rain outside, quiet servo
movement, one restrained electrical pulse, deep lens ignition hum, then calm.
"""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--character", type=Path, required=True)
    parser.add_argument("--prop", type=Path, required=True)
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    return parser.parse_args()


def _configure(output_root: Path) -> None:
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY is required")
    data_root = output_root / "runtime-data"
    data_root.mkdir(parents=True, exist_ok=True)
    os.environ["CREATOR_DATA_ROOT"] = str(data_root)
    os.environ["IMAGE_MODEL"] = "DASHSCOPE"
    os.environ["DASHSCOPE_IMAGE_API_KEY"] = key
    os.environ["DASHSCOPE_IMAGE_MODEL_NAME"] = "qwen-image-3.0-pro"
    os.environ["DASHSCOPE_IMAGE_TIMEOUT"] = "900"
    os.environ["VIDEO_API_KEY"] = key
    os.environ["VIDEO_MODEL_NAME"] = "happyhorse-1.1"
    os.environ["VIDEO_BASE_URL"] = "https://dashscope.aliyuncs.com/api/v1"


async def _run(args: argparse.Namespace) -> Path:
    from models.image import generate_image
    from models.video_model import check_task_status, submit_video_task
    from services.media_files.image_execution import (
        _append_storyboard_panel_aspect_contract,
    )
    from utils.paths import media_path_from_url, media_task_scope
    from utils.remote_download import download_remote_file

    root = args.output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    _configure(root)
    references = [
        args.character.expanduser().resolve(),
        args.prop.expanduser().resolve(),
        args.scene.expanduser().resolve(),
    ]
    for reference in references:
        if not reference.is_file():
            raise FileNotFoundError(reference)

    compiled_storyboard_prompt = _append_storyboard_panel_aspect_contract(
        STORYBOARD_PROMPT,
        "16:9",
        3,
    )
    with media_task_scope("real-contract-storyboard"):
        generated = await generate_image(
            compiled_storyboard_prompt,
            aspect_ratio="16:9",
            reference_image_urls=[path.as_uri() for path in references],
        )
    local = media_path_from_url(str(generated["url"]))
    storyboard_path = root / "corrected-storyboard.png"
    shutil.copy2(local, storyboard_path)

    ordered_video_refs = [storyboard_path, *references]
    provider_task_id = await submit_video_task(
        prompt=VIDEO_PROMPT,
        reference_image_url_list=[path.as_uri() for path in ordered_video_refs],
        ratio="16:9",
        duration=3,
        resolution="720P",
        watermark=False,
        generate_audio=True,
        mode="r2v",
    )
    deadline = time.monotonic() + args.timeout_seconds
    provider_result = None
    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        provider_result = await check_task_status(provider_task_id)
        status = str(provider_result.get("status") or "").upper()
        if status == "SUCCEEDED":
            break
        if status == "FAILED":
            raise RuntimeError(
                f"HappyHorse regression failed: {provider_result.get('error')}",
            )
    else:
        raise TimeoutError("HappyHorse regression timed out")

    result_url = str((provider_result or {}).get("result_url") or "")
    if not result_url:
        raise RuntimeError("HappyHorse succeeded without result URL")
    video_path = root / "corrected-3s-reference-order.mp4"
    await asyncio.to_thread(download_remote_file, result_url, str(video_path))
    report = {
        "ok": True,
        "created_at": datetime.now(UTC).isoformat(),
        "models": {
            "image": "qwen-image-3.0-pro",
            "video": "happyhorse-1.1-r2v",
        },
        "duration_requested_seconds": 3,
        "aspect_ratio": "16:9",
        "storyboard_contract": {
            "panel_count": 3,
            "lattice": "2x2",
            "blank_slots": 1,
            "panel_inner_ratio": "16:9",
            "single_character_instance_per_panel": True,
        },
        "happyhorse_reference_order": [
            "[Image 1]=corrected storyboard",
            "[Image 2]=character identity",
            "[Image 3]=prop identity",
            "[Image 4]=scene identity",
        ],
        "provider_task_id": provider_task_id,
        "storyboard": str(storyboard_path),
        "video": str(video_path),
        "storyboard_prompt": compiled_storyboard_prompt,
        "video_prompt": VIDEO_PROMPT,
    }
    report_path = root / "regression-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report_path


def main() -> None:
    args = _arguments()
    report = asyncio.run(_run(args))
    print(f"regression report: {report}")


if __name__ == "__main__":
    main()
