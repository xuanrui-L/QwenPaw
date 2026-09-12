# -*- coding: utf-8 -*-
"""IVB 包格式层:阅读器 + 校验器。与放映服务、状态存储完全解耦。"""

from __future__ import annotations

from .errors import CODEBOOK, Diagnostic, Severity
from .model import (
    MAX_SUPPORTED_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    Bundle,
    EdgeInfo,
    InteractionPoint,
    NodeInfo,
    Presentation,
    Theme,
)
from .reader import (
    BundleError,
    BundleSource,
    DirBundleSource,
    Inspection,
    ZipBundleSource,
    inspect_bundle,
    open_source,
    read_bundle,
)
from .validate import validate_bundle

__all__ = [
    "CODEBOOK",
    "Bundle",
    "BundleError",
    "BundleSource",
    "Diagnostic",
    "DirBundleSource",
    "EdgeInfo",
    "Inspection",
    "InteractionPoint",
    "MAX_SUPPORTED_SCHEMA_VERSION",
    "MIN_SUPPORTED_SCHEMA_VERSION",
    "NodeInfo",
    "Presentation",
    "Severity",
    "Theme",
    "ZipBundleSource",
    "inspect_bundle",
    "open_source",
    "read_bundle",
    "validate_bundle",
]
