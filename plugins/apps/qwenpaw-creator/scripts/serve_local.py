#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the real Creator runtime and player for one local OS account.

Build ui/ first, then run with --data-dir pointing to persistent local data.
The service binds only to loopback. No project or media fixtures are created.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--player-data-dir", type=Path)
    parser.add_argument("--port", type=int, default=19000)
    args = parser.parse_args()
    app_root = Path(__file__).resolve().parents[1]
    data = args.data_dir.expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    os.environ["CREATOR_DATA_ROOT"] = str(data)
    os.environ["CREATOR_MODEL_CONFIG_PATH"] = str(
        data / "config" / "model_config.json",
    )
    os.environ.setdefault("QWENPAW_WORKING_DIR", str(data / "local-settings"))
    os.environ.setdefault("QWENPAW_SECRET_DIR", str(data / "local-secrets"))
    sys.path[:0] = [
        str(app_root / "backend"),
        str(app_root / "player"),
        str(app_root.parents[2] / "src"),
    ]

    # Import after selecting this instance's persistent data root.
    from dev_main import app  # pylint: disable=import-outside-toplevel
    from ivb.server.app import (  # pylint: disable=import-outside-toplevel
        create_app,
    )

    # pylint: disable=import-outside-toplevel
    from starlette.staticfiles import (
        StaticFiles,
    )
    import uvicorn  # pylint: disable=import-outside-toplevel

    app.mount(
        "/creator-ui",
        StaticFiles(directory=app_root / "ui" / "dist" / "app", html=True),
    )
    app.mount(
        "/",
        create_app(
            args.player_data_dir or data / "player",
            local_owner=f"local:{getpass.getuser()}",
        ),
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
