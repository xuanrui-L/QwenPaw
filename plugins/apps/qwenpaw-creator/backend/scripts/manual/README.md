# Manual Creator acceptance runners

These scripts are billed, operator-run acceptance evidence rather than CI test
fixtures. They use real configured providers, import Creator service internals,
and keep sanitized reports under a temporary data root.

- `run_real_short_drama_e2e.py` runs the three-second or one-minute Creator
  short-drama flow with qwen-image3 and HappyHorse.
- `run_real_prompt_contract_regression.py` replays the focused storyboard to
  R2V prompt-contract check against approved local identity assets.

Run them from the repository root and provide credentials only through the
documented environment variables. Never commit keys or generated media.
