# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ivb_player.testing import (  # noqa: E402
    BundleSpec,
    write_bundle_dir,
    write_bundle_zip,
)  # noqa: E402


@pytest.fixture
def spec() -> BundleSpec:
    return BundleSpec()


@pytest.fixture
def bundle_dir(tmp_path, spec):
    return write_bundle_dir(tmp_path / "clean.ivb", spec)


@pytest.fixture
def bundle_zip(tmp_path, spec):
    return write_bundle_zip(tmp_path / "clean.ivb.zip", spec)


@pytest.fixture
def make_zip(tmp_path):
    def _make(name: str, **kwargs) -> Path:
        return write_bundle_zip(tmp_path / f"{name}.zip", BundleSpec(**kwargs))

    return _make


@pytest.fixture
def make_dir(tmp_path):
    def _make(name: str, **kwargs) -> Path:
        return write_bundle_dir(tmp_path / name, BundleSpec(**kwargs))

    return _make
