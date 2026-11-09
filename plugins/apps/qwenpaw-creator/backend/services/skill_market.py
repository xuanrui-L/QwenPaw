# -*- coding: utf-8 -*-
"""Market search adapter for Creator skills.

Reuses the host QwenPaw market infrastructure (``qwenpaw.market.service``)
so that Creator's skill browser sees the same providers and results as the
main console's market page. Only reading/searching is delegated; install
still flows through ``services.skill_hub``, so the single-SKILL.md-only
rule stays enforced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_MAX_MARKET_LIMIT = 20
_DEFAULT_PAGE = 1


@dataclass(frozen=True)
class MarketSkillResult:
    source: str
    source_label: str | None
    slug: str
    name: str
    description: str | None
    source_url: str
    version: str | None
    author: str | None
    icon_url: str | None
    stats: dict[str, str | int] | None


@dataclass(frozen=True)
class MarketProviderInfo:
    key: str
    label: str
    available: bool
    reason: str | None
    supports_browse: bool


class MarketUnavailable(RuntimeError):
    """The running QwenPaw build exposes no market search capability."""


def _resolve_market_service():
    try:
        from qwenpaw.market.service import (  # pylint: disable=import-outside-toplevel
            list_providers,
            search_market,
        )
    except Exception as exc:  # noqa: BLE001 - host layout, not our bug
        logger.warning("market service unavailable: %s", exc)
        raise MarketUnavailable(
            "当前 QwenPaw 运行时未提供技能市场搜索能力，"
            "请改用 URL 导入或手动创建技能",
        ) from exc
    return list_providers, search_market


async def search_skills(
    query: str = "",
    page: int = _DEFAULT_PAGE,
    limit: int = _MAX_MARKET_LIMIT,
    lang: str = "en",
) -> tuple[list[MarketSkillResult], bool, dict[str, int]]:
    """Search skill market providers; returns results, has_more, totals."""
    _, search_market_fn = _resolve_market_service()
    capped_limit = max(1, min(int(limit or _MAX_MARKET_LIMIT), _MAX_MARKET_LIMIT))
    page_num = max(1, int(page or _DEFAULT_PAGE))
    from qwenpaw.market.registry import market_registry  # pylint: disable=import-outside-toplevel

    # Build provider_pages for all available providers so the user
    # sees results from every market in one call.
    provider_pages: dict[str, int] = {}
    for key, provider in market_registry.snapshot().items():
        is_available, _reason = provider.available()
        if is_available and provider.supports_browse:
            provider_pages[key] = page_num

    results, errors, by_provider = await search_market_fn(
        query=query,
        provider_pages=provider_pages,
        limit=capped_limit,
        lang=lang,
    )
    for err in errors:
        logger.warning(
            "market provider %s failed: %s", err.provider, err.message,
        )

    converted = [
        MarketSkillResult(
            source=r.source,
            source_label=getattr(r, "source_label", None),
            slug=r.slug,
            name=r.name,
            description=r.description,
            source_url=r.source_url,
            version=r.version,
            author=r.author,
            icon_url=r.icon_url,
            stats=dict(r.stats) if r.stats else None,
        )
        for r in results
    ]

    has_more = any(has for has, _total in by_provider.values())
    totals: dict[str, int] = {
        key: total for key, (_has, total) in by_provider.items()
    }
    return converted, has_more, totals


def list_market_providers() -> list[MarketProviderInfo]:
    """List registered market providers and their status."""
    list_providers_fn, _ = _resolve_market_service()
    raw = list_providers_fn()
    return [
        MarketProviderInfo(
            key=p.key,
            label=p.label,
            available=p.available,
            reason=p.reason,
            supports_browse=p.supports_browse,
        )
        for p in raw
    ]


__all__ = [
    "MarketProviderInfo",
    "MarketSkillResult",
    "MarketUnavailable",
    "list_market_providers",
    "search_skills",
]