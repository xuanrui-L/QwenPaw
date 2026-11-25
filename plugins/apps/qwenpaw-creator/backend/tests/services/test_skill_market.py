# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Hosted skill discovery, download, and error handling."""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from dataclasses import dataclass

import pytest

from services import skill_hub, skill_market

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Provider:
    """Stands in for the host's ``ProviderInfo``."""

    key: str
    label: str = "Market"
    available: bool = True
    reason: str | None = None
    supports_browse: bool = True


@dataclass(frozen=True)
class _Result:
    """Stands in for the host's ``MarketResult``."""

    source: str = "acme"
    slug: str = "demo-skill"
    name: str = "Demo"
    description: str | None = "does the thing"
    source_url: str = "https://market.example/acme/demo-skill"
    version: str | None = "1.0.0"
    author: str | None = "acme"
    icon_url: str | None = None
    stats: dict | None = None


@dataclass(frozen=True)
class _Error:
    """Stands in for the host's ``MarketSearchError``."""

    provider: str = "acme"
    message: str = "boom"


def _install_fake_service(monkeypatch, providers, capture):
    """Publish a fake ``qwenpaw.market.service`` and record its calls."""

    async def search_market(
        query,
        provider_pages,
        limit=10,
        lang="en",
        category=None,
    ):
        capture["query"] = query
        capture["provider_pages"] = provider_pages
        capture["limit"] = limit
        capture["lang"] = lang
        return (
            [_Result()],
            [_Error()],
            {"acme": (True, 42)},
        )

    module = types.ModuleType("qwenpaw.market.service")

    def list_providers():
        capture["providers_thread"] = threading.current_thread()
        return list(providers)

    module.list_providers = list_providers
    module.search_market = search_market
    monkeypatch.setitem(sys.modules, "qwenpaw.market.service", module)


def _run(coro):
    return asyncio.run(coro)


def test_search_survives_a_host_without_a_provider_registry(
    monkeypatch,
) -> None:
    """A release line shipping no market registry still searches.

    ``sys.modules`` set to None makes any import of the registry raise, so a
    stray hard dependency would surface here as an ImportError instead of a
    result.  This is the regression guard for that coupling.
    """

    monkeypatch.setitem(sys.modules, "qwenpaw.market.registry", None)
    capture: dict = {}
    _install_fake_service(monkeypatch, [_Provider("acme")], capture)

    results, has_more, totals = _run(skill_market.search_skills("demo"))

    assert capture["provider_pages"] == {"acme": 1}
    assert [r.slug for r in results] == ["demo-skill"]
    assert has_more is True
    assert totals == {"acme": 42}


def test_provider_discovery_runs_off_the_event_loop(
    monkeypatch,
) -> None:
    """Asking providers whether they are usable never blocks the loop.

    ``available()`` can read the environment and import an optional backend.
    The providers route already offloads that lookup; search has to match, or
    one slow provider would stall every other request in the process.
    """

    capture: dict = {}
    _install_fake_service(monkeypatch, [_Provider("acme")], capture)

    async def scenario() -> threading.Thread:
        loop_thread = threading.current_thread()
        await skill_market.search_skills("demo")
        return loop_thread

    loop_thread = _run(scenario())

    assert capture["providers_thread"] is not loop_thread


def test_unavailable_and_unbrowsable_providers_are_excluded(
    monkeypatch,
) -> None:
    """Only markets that can answer a paged browse are asked."""

    capture: dict = {}
    _install_fake_service(
        monkeypatch,
        [
            _Provider("live", available=True, supports_browse=True),
            _Provider("down", available=False, supports_browse=True),
            _Provider("search_only", available=True, supports_browse=False),
        ],
        capture,
    )

    _run(skill_market.search_skills("demo", page=3))

    assert capture["provider_pages"] == {"live": 3}


def test_provider_filter_narrows_the_searched_markets(monkeypatch) -> None:
    """The source picker narrows which markets answer, and can never pull an
    unavailable one back in or drop the default when left empty."""

    capture: dict = {}
    _install_fake_service(
        monkeypatch,
        [
            _Provider("live", available=True, supports_browse=True),
            _Provider("other", available=True, supports_browse=True),
            _Provider("down", available=False, supports_browse=True),
        ],
        capture,
    )

    _run(skill_market.search_skills("demo", providers=["live"]))
    assert capture["provider_pages"] == {"live": 1}

    # Naming an unavailable market does not force it back into the search.
    _run(skill_market.search_skills("demo", providers=["live", "down"]))
    assert capture["provider_pages"] == {"live": 1}

    # An empty filter is the default: every reachable market is searched.
    _run(skill_market.search_skills("demo", providers=[]))
    assert capture["provider_pages"] == {"live": 1, "other": 1}


def test_limit_is_capped_and_page_floor_applied(monkeypatch) -> None:
    """The adapter owns the caps, so a huge client page cannot leak through."""

    capture: dict = {}
    _install_fake_service(monkeypatch, [_Provider("acme")], capture)

    _run(skill_market.search_skills("demo", page=0, limit=9999))

    assert capture["limit"] == skill_market._MAX_MARKET_LIMIT
    assert capture["provider_pages"] == {"acme": 1}


def test_missing_market_service_becomes_a_clean_refusal(monkeypatch) -> None:
    """A host without market search fails as 501 material, not a crash."""

    monkeypatch.setitem(sys.modules, "qwenpaw.market.service", None)
    with pytest.raises(skill_market.MarketUnavailable):
        _run(skill_market.search_skills("demo"))
    with pytest.raises(skill_market.MarketUnavailable):
        _run(asyncio.to_thread(skill_market.list_market_providers))


def test_provider_listing_maps_host_fields(monkeypatch) -> None:
    """The UI sees the host's availability and browse flags verbatim."""

    capture: dict = {}
    _install_fake_service(
        monkeypatch,
        [_Provider("acme", label="Acme", available=False, reason="no key")],
        capture,
    )

    info = skill_market.list_market_providers()[0]

    assert info.key == "acme"
    assert info.label == "Acme"
    assert info.available is False
    assert info.reason == "no key"
    assert info.supports_browse is True


class _Payload:
    """Stands in for the host's private ``_InstallPayload``."""

    name = "hub-skill"
    content = "---\nname: hub-skill\n---\n\n# Hub\n"
    source_url = "https://skills.sh/acme/skills/hub-skill"
    installed_from = "skills-sh"
    references = {"a.md": "1", "deep": {"b.md": "2", "c.md": "3"}}
    scripts = {"run.sh": "echo"}
    extra_files: dict = {}


@pytest.mark.parametrize(
    "url",
    (
        "",
        "skills.sh/acme/skills/hub-skill",
        "ftp://example.com/SKILL.md",
        "file:///etc/passwd",
        "https:///no-host",
        "https://user:secret@example.com/SKILL.md",
        "https://example.com/" + "a" * 3000,
    ),
)
def test_refused_urls_never_reach_the_fetcher(url, monkeypatch) -> None:
    """URL shape is decided locally, before anything is resolved or fetched."""

    def forbidden():
        raise AssertionError("the fetcher must not be resolved")

    monkeypatch.setattr(skill_hub, "_resolve_fetcher", forbidden)
    with pytest.raises(skill_hub.SkillHubUrlError):
        asyncio.run(skill_hub.fetch_skill_bundle(url))


def test_missing_host_hub_becomes_a_clean_refusal(monkeypatch) -> None:
    """A host without the hub fetch fails as 501 material, not a crash.

    ``sys.modules`` set to None makes the import itself raise, which is how a
    renamed or dropped private helper would look at request time.
    """

    monkeypatch.setitem(sys.modules, "qwenpaw.agents.skill_system.hub", None)
    with pytest.raises(skill_hub.SkillHubUnavailable):
        asyncio.run(
            skill_hub.fetch_skill_bundle(
                "https://skills.sh/acme/skills/hub-skill",
            ),
        )


def test_slow_market_becomes_a_timeout(monkeypatch) -> None:
    """The 90s budget is a stated refusal rather than a hung request."""

    async def slow(_bundle_url, _version, _target_name):
        await asyncio.sleep(1)
        return _Payload()

    monkeypatch.setattr(skill_hub, "_resolve_fetcher", lambda: slow)
    monkeypatch.setattr(skill_hub, "_HUB_IMPORT_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(skill_hub.SkillHubTimeout):
        asyncio.run(
            skill_hub.fetch_skill_bundle(
                "https://skills.sh/acme/skills/hub-skill",
            ),
        )


def test_bundle_is_normalised_and_attached_files_counted(monkeypatch) -> None:
    """Only SKILL.md travels on; the rest is counted, not written."""

    async def fetcher(_bundle_url, _version, _target_name):
        return _Payload()

    monkeypatch.setattr(skill_hub, "_resolve_fetcher", lambda: fetcher)
    bundle = asyncio.run(
        skill_hub.fetch_skill_bundle(
            "https://skills.sh/acme/skills/hub-skill",
        ),
    )
    assert bundle.name == "hub-skill"
    assert bundle.content == _Payload.content
    assert bundle.source_url == _Payload.source_url
    assert bundle.installed_from == "skills-sh"
    # references (3 leaves, one nested) + scripts (1).
    assert bundle.ignored_files == 4


def test_target_name_is_forwarded_to_the_host(monkeypatch) -> None:
    """Pinning the name stays the host's decision, not a second rule here."""

    seen: list[tuple] = []

    async def fetcher(bundle_url, version, target_name):
        seen.append((bundle_url, version, target_name))
        return _Payload()

    monkeypatch.setattr(skill_hub, "_resolve_fetcher", lambda: fetcher)
    asyncio.run(
        skill_hub.fetch_skill_bundle(
            "https://skills.sh/acme/skills/hub-skill",
            version="1.2.3",
            target_name="pinned-skill",
        ),
    )
    assert seen == [
        (
            "https://skills.sh/acme/skills/hub-skill",
            "1.2.3",
            "pinned-skill",
        ),
    ]
