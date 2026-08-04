# Third-Party Vendored Code Notices

This directory contains algorithm code vendored into QwenPaw Creator under
the terms of the original projects' licenses. Each vendored file keeps an
attribution header and marks local modifications (Apache-2.0 §4b).

## Qwen-MM-Plugins

- Source repository: Qwen-MM-Plugins
- Commit: `077aea63d9e7ad50d91bab6c8dff12183a24d48b` (`077aea6`)
- License: Apache License 2.0 (see the upstream repository `LICENSE`)
- Vendored location: `backend/vendor/mm_plugins/`

### Vendored modules (video-memory)

Ported from `src/capabilities/video-memory/` into
`backend/vendor/mm_plugins/video_memory/`:

| Vendored file | Upstream origin | Modifications |
|---|---|---|
| `schema.py` | `skill/script/build_memory/schema.py` | dropped the legacy `hierarchical_graph_final.json` loader; comments trimmed |
| `prompts.py` | `skill/script/build_memory/prompts.py` | verbatim prompt constants |
| `time_utils.py` | `skill/script/build_memory/time_utils.py` | verbatim |
| `json_utils.py` | `skill/script/build_memory/llm_client.py` (`extract_json` only) | extracted the JSON-repair parser; the HTTP client itself is NOT vendored (rewritten as Creator-native clients) |
| `segmentation.py` | `skill/script/build_memory/build_graph.py` (Phase 1) | frame extraction/IO moved to the Creator service; OpenCV HLS conversion re-implemented with Pillow + NumPy; pure planning functions kept |
| `subgraph.py` | `skill/script/build_memory/build_graph.py` (Phase 2 parsing) | media clipping/upload and VLM transport removed (Creator `creator_vlm_model` backend is used instead); response parsing and relative→absolute time shifting kept |
| `aggregation.py` | `skill/script/build_memory/build_graph.py` (Phase 3) + `pipeline_worker.py` orchestration ideas | orchestration rewritten as `async` around an injected LLM callable; window/parse/fallback logic kept |
| `embeddings.py` | `skill/script/build_memory/embeddings.py` | DashScope HTTP client removed (rewritten as `backend/models/embedding_model.py`); BM25 index, hybrid RRF search and `.npz` persistence kept; search accepts a precomputed query embedding; tokenizer splits CJK runs into character bigrams for exact short-phrase BM25 matches; zero-score BM25 candidates no longer earn a sparse RRF rank |
| `toolkit.py` | `qwen_mm_plugins_video_memory/toolkit.py` + query logic of `loader.py` | EgoLife time system and cutoff support removed; methods return Python objects instead of JSON strings; embedding lookups take a precomputed query vector |

Not vendored: `llm_client.py`, `env_config.py` (env-driven configuration is
replaced by the Creator config tree), `merge_memories.py` (not in scope), the
MCP server wrappers under `qwen_mm_plugins_video_memory/tools/` (their query
logic is reachable through the vendored `toolkit.py`).
