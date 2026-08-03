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
| `mm_plugins/image_budget.py` | `src/shared/image.py` (`budget_to_pixels`, `smart_resize`) + constants from `src/shared/env.py` | Constants inlined (no env lookups); `smart_resize` floors the over-budget branch (matching canonical `qwen_vl_utils`) so results never exceed the pixel budget. Shared with the self-review worktree; keep content byte-identical across branches. |
| `mm_plugins/renderers/__init__.py` | `src/capabilities/core/qwen_mm_plugins_core/renderers/__init__.py` | Registry trimmed to the formats Creator ships (no latex/model3d/geo/drawio/blender); renderers emit PIL images + meta blocks instead of base64 MCP blocks; `cached_pdf` replaced by caller-owned temp dirs; added `configure_matplotlib_cjk` so table images keep CJK text. |
| `mm_plugins/renderers/pdf.py` | `.../renderers/pdf.py` | Returns PIL page images with page numbers and text layers; response-size capping and base64 encoding removed (resizing/persistence is done by `services/document_reader.py`). |
| `mm_plugins/renderers/office.py` | `.../renderers/office.py` | `which_tool` lookup replaced by an explicit `soffice` executable argument resolved by Creator's runtime-dependency layer; conversion timeout raised to cover the macOS first-launch Gatekeeper scan. |
| `mm_plugins/renderers/data.py` | `.../renderers/data.py` | Emits PIL table images + markdown text blocks; sheet ordinal exposed as page number; table rendering prefers an installed CJK font. |
| `mm_plugins/renderers/subtitle.py` | `.../renderers/subtitle.py` | Added a minimal `.ass` dialogue parser (upstream supports SRT/VTT only). |
| `mm_plugins/renderers/code.py` | `.../renderers/code.py` | Verbatim aside from block meta additions. |
| `mm_plugins/renderers/svg.py` | `.../renderers/svg.py` + `svg_to_image` from `src/shared/image.py` | `svg_to_image` inlined; returns a PIL image block. |
| `mm_plugins/renderers/notebook.py` | `.../renderers/notebook.py` | Output images decoded to PIL; base64 passthrough removed. |
| `mm_plugins/renderers/web.py` | `.../renderers/web.py` | Kept Playwright screenshot flow; enablement is config-gated by Creator (`CREATOR_DOC_READER_WEB_ENABLED`), Playwright is not a declared dependency. |

Formats intentionally not vendored: `latex`, `model3d`, `geo`, `drawio`,
`_blender_render`.
