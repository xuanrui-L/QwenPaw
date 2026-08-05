# Third-Party Vendored Code Notice

This directory contains source code vendored (algorithm ported) from
third-party projects under their respective licenses. Vendoring follows the
project-wide rule set in
`docs/proposals/mm-plugins-creator-integration/MM_PLUGINS_INTEGRATION_PLAN.md`
(§2.2, technique B): no runtime dependency on the upstream package, no
environment-variable injection, no in-process invocation of upstream code.

## Qwen-MM-Plugins

- Upstream repository: Qwen-MM-Plugins
  (local mirror: `/Users/linxuanrui/Documents/projects/Project-Creator/Qwen-MM-Plugins`)
- Upstream baseline: `release` branch, commit `077aea6`
- License: Apache License 2.0 (see upstream `LICENSE`)
- Vendored location: `backend/vendor/mm_plugins/`

Per Apache-2.0 §4(b), every vendored file keeps an attribution header naming
the upstream path and commit, and states that the file carries modifications
for QwenPaw Creator. The canonical header template is:

```python
# Vendored from Qwen-MM-Plugins (Apache-2.0), release commit 077aea6.
# Upstream path: <path inside upstream repository>
# Modified for QwenPaw Creator. See backend/vendor/NOTICE.md.
```

### Module inventory

| Vendored module | Upstream source | Modifications |
| --- | --- | --- |
| `mm_plugins/image_budget.py` | `src/shared/image.py` (`budget_to_pixels`, `smart_resize`) + constants from `src/shared/env.py` | Constants inlined (no env lookups); `smart_resize` floors the over-budget branch (matching canonical `qwen_vl_utils`) and shrinks the long side after short-side clamping so results never exceed the pixel budget, even at extreme aspect ratios. Shared with the doc-reader worktree; keep content byte-identical across branches. Render-review (WT4) adds the `VIDEO_BUDGET_TOKENS`/`VIDEO_MIN_PIXELS` constants as a pure additive block on top of the canonical copy. |
| `mm_plugins/review_rubrics.py` | `src/capabilities/video-edit/skill/review/final-review.md` (§D Appeal rubric `[rubric-verbatim]` rows, common failures), `review/scene-review.md` (six checks), `review/source-review.md` (technical probe fields) | Markdown tables ported to structured constants; row names and anchor questions kept verbatim. The upstream concept-veto ("<=5 caps the verdict at revise") is NOT ported — Creator run review is advisory, a weak concept becomes a major-severity suggestion. |
| `mm_plugins/review_gates.py` | `src/capabilities/video-edit/skill/scripts/black_check.sh`, `loudness_check.sh`, `review_gate.sh` | Shell gates ported to Python with thresholds preserved (blackdetect d=0.1/pix_th=0.10/grace=0.5; -inf and <-50 LUFS silence; -10/-24/-1.0 advisories; ffprobe→loudness→black order and the evidence-block hash). The upstream `exit 2` delivery-blocking semantics and plan gate are not ported — gate failures become advisory findings. |
| `mm_plugins/frame_stats.py` | `src/capabilities/video-edit/skill/scripts/auto_grade.py` (`probe_duration`, `sample_stats`, analyze judgments) | Analyze side only; `sys.exit` becomes `FrameStatsError`; still-image helper added. The eq-filter derivation/apply mode is intentionally not vendored (grading stays an opt-in art-direction decision). |
