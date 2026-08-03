# Render Review Eval Set

Curated cases for the six-dimension render self-review prompt
(`services/render_review/protocol.py`). Each `cases/<name>/` directory holds:

- `video.mp4` — the clip under review (historical final renders are
  re-encoded copies; defect clips are constructed with ffmpeg from a
  historical slice).
- `expected.json` — human-annotated ground truth:
  - `plan_context` — passed to the reviewer as the plan context;
  - `expected_failures` — dimensions that MUST be flagged (missing one is a
    missed defect);
  - `acceptable_extra_failures` — dimensions that may be flagged without
    counting as a false alarm (verified against the actual footage);
  - any other flagged dimension counts as a false alarm.

Iteration bar (enforced by `tests/render_review/test_eval_set.py`): zero
missed defects across the set, at most one false alarm per case.

Run (manual, real VLM, costs tokens):

```bash
RENDER_REVIEW_EVAL=1 CREATOR_DATA_ROOT=... QWENPAW_KEYRING_ACCOUNT=... \
    pytest tests/render_review/test_eval_set.py -m manual -s
```

| Case | Type | Defect |
|---|---|---|
| historical-drama | historical render (creative-generation) | none (all-pass) |
| historical-cat-vlog | historical render (material-editing) | none (static-tail pacing flag tolerated) |
| defect-black-frames | constructed | 1.5s interior black at 8.0-9.5s |
| defect-av-desync | constructed | audio delayed 1.5s |
| defect-subtitle-overflow | constructed | caption running past the frame edge, 4-20s |
| defect-missing-voiceover | constructed | audio track muted to silence |
| defect-pacing-drag | constructed | frame frozen 8.0-15.2s |
