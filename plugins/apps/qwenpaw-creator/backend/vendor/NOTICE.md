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
