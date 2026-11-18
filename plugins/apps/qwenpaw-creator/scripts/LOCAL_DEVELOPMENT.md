# Local Creator and interactive playback

Build the UI in `../ui` with `npm run build`, then run from the plugin directory:

```sh
python scripts/serve_local.py --data-dir /absolute/persistent/creator-data --port 19000
```

Use a Python environment with the Creator and player dependencies installed.
Open `http://127.0.0.1:19000/creator-ui/` to configure models and create a project.
Open `http://127.0.0.1:19000/` to import the exported interactive ZIP and play it.
Both interfaces use production routes, generation services and validation.
The launcher creates no demo project, generated media, or authored HTML.

This is an explicit single-user local service. It only binds to `127.0.0.1`.
The player assigns uploads and progress to the OS account running the process,
ignoring browser identity headers. The regular IVB deployment still requires
its trusted upstream identity for uploads. Do not use this local launcher as
a public gateway.

Keep the data directory and its secret store between restarts. The default
player library lives in `creator-data/player`; `--player-data-dir` can retain
an existing library. Restarting with the same directories and port preserves
project URLs, imported bundles and progress. Model settings are stored at
`creator-data/config/model_config.json`; its encrypted credentials must stay
with the same secret store. Configure credentials through the model settings
screen or the documented provider environment variables.

Qwen3 Agent and review requests default to a 4,096-token reasoning allowance.
This limits time spent before the model responds; it does not cap generated
script/tool output. Set `CREATOR_AGENT_THINKING_BUDGET` (1–32,768) before starting
the service when a larger planning allowance is needed. This uses the provider's
documented [thinking budget](https://help.aliyun.com/zh/model-studio/deep-thinking)
and is not sent to other compatible model families.

For a link that must remain usable after closing a terminal, run this command
under the OS service manager (for example a macOS LaunchAgent with `KeepAlive`
and `RunAtLoad`, or a systemd user service with `Restart=on-failure`). Store
stdout/stderr logs outside the source checkout. An interactive terminal or
agent command session by itself does not guarantee continued availability.

Acceptance must include reloading the player after a service restart, actually
playing the generated video, following each branch, using the story map, and
restarting the story. HTTP success and a valid ZIP alone do not verify playback.
