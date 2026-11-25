# -*- coding: utf-8 -*-
"""Recovery when Windows rejects an archived access path."""

from test_project_import_export import _assert_windows_archive_recovery


def test_windows_archive_recovery_survives_an_invalid_path_error(
    tmp_path, monkeypatch
):
    _assert_windows_archive_recovery(
        tmp_path,
        monkeypatch,
        True,
        invalid_path_error=True,
    )
