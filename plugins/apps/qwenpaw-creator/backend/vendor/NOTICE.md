# Third-Party Notices — `backend/vendor/`

This directory contains code vendored from third-party repositories under
Apache-2.0-compliant terms (Apache License 2.0, Section 4). Each vendored
file keeps the upstream attribution in its header together with a summary of
local modifications.

## Qwen-MM-Plugins

- Source repository: https://github.com/QwenLM/Qwen-MM-Plugins
- Vendored at commit: `077aea63d9e7ad50d91bab6c8dff12183a24d48b`
- License: Apache License, Version 2.0
  (https://www.apache.org/licenses/LICENSE-2.0)

Vendored modules:

| Local module | Upstream source | Notes |
|---|---|---|
| `vendor/mm_plugins/image_budget.py` | `src/shared/image.py` (`budget_to_pixels`, `smart_resize`) + budget constants from `src/shared/env.py` | Function bodies verbatim; constants inlined to drop the upstream package dependency. |
