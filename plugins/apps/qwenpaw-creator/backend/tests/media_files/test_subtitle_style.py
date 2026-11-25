# -*- coding: utf-8 -*-
"""Subtitle packs, renderer overrides, and saved user styles."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.subtitle_style_routes import router
from services.media_files import overlay
from services.media_files.motion_blueprints import (
    CAPTION_BLUEPRINTS,
    SUBTITLE_TEMPLATE_PACK_ORDER,
    SUBTITLE_TEMPLATE_PACKS,
    blueprint_catalog_text,
    content_type_caption_order,
    subtitle_pack_caption_order,
    subtitle_pack_preview_specs,
)
from services.media_files.motion_templates import (
    CAPTION_ENTRANCES,
    render_caption_template,
)
from services.media_files.overlay import (
    _find_cjk_font,
    _resolve_subtitle_font,
    _subtitle_pack_painter,
    _subtitle_style_values,
)
from services.project_files.models import ProjectSettings, SubtitleStyle


class TestSubtitleStyleValues:
    def test_missing_style_keeps_renderer_defaults(self) -> None:
        assert _subtitle_style_values(None) == ("#FFFFFF", 1.0, 1.0)
        assert _subtitle_style_values({}) == ("#FFFFFF", 1.0, 1.0)

    def test_valid_colour_is_preserved(self) -> None:
        color, _, _ = _subtitle_style_values({"color": "#ffdd00"})
        assert color == "#ffdd00"

    @pytest.mark.parametrize(
        "raw",
        ["white", "#fff", "#gggggg", "", None, 123],
    )
    def test_malformed_colour_falls_back_to_white(self, raw: object) -> None:
        color, _, _ = _subtitle_style_values({"color": raw})
        assert color == "#FFFFFF"

    def test_scales_are_clamped_to_safe_ranges(self) -> None:
        _, font, stroke = _subtitle_style_values(
            {"font_scale": 5.0, "stroke_scale": -2.0},
        )
        assert font == 1.8
        assert stroke == 0.0

        _, font, stroke = _subtitle_style_values(
            {"font_scale": 0.05, "stroke_scale": 99.0},
        )
        assert font == 0.6
        assert stroke == 3.0

    def test_zero_stroke_disables_the_outline(self) -> None:
        _, _, stroke = _subtitle_style_values({"stroke_scale": 0})
        assert stroke == 0.0

    @pytest.mark.parametrize("raw", ["big", None, float("nan")])
    def test_non_numeric_scales_keep_defaults(self, raw: object) -> None:
        _, font, stroke = _subtitle_style_values(
            {"font_scale": raw, "stroke_scale": raw},
        )
        assert font == 1.0
        assert stroke == 1.0


class TestSubtitleStyleModel:
    def test_settings_default_to_no_override(self) -> None:
        settings = ProjectSettings()
        assert settings.subtitle_template is None
        assert settings.subtitle_style is None
        assert SubtitleStyle().background_opacity is None

    def test_style_round_trips_through_the_settings_document(self) -> None:
        settings = ProjectSettings(
            subtitle_template="variety_pop",
            subtitle_style=SubtitleStyle(
                color="#FFE066",
                font_scale=1.25,
                stroke_scale=0.0,
            ),
        )
        restored = ProjectSettings.model_validate(
            settings.model_dump(mode="json"),
        )
        assert restored.subtitle_template == "variety_pop"
        assert restored.subtitle_style is not None
        assert restored.subtitle_style.color == "#FFE066"
        assert restored.subtitle_style.font_scale == 1.25
        assert restored.subtitle_style.stroke_scale == 0.0

    def test_font_family_defaults_to_none_and_round_trips(self) -> None:
        assert SubtitleStyle().font_family is None
        settings = ProjectSettings(
            subtitle_style=SubtitleStyle(font_family="song"),
        )
        restored = ProjectSettings.model_validate(
            settings.model_dump(mode="json"),
        )
        assert restored.subtitle_style is not None
        assert restored.subtitle_style.font_family == "song"

    def test_unknown_font_family_is_rejected(self) -> None:
        with pytest.raises(Exception):
            SubtitleStyle.model_validate({"font_family": "comic-sans"})

    @pytest.mark.parametrize(
        "payload",
        [
            {"color": "red"},
            {"font_scale": 4.0},
            {"stroke_scale": -0.5},
            {"background_opacity": 1.5},
            {"background_opacity": -0.2},
        ],
    )
    def test_out_of_range_values_are_rejected(self, payload: dict) -> None:
        with pytest.raises(Exception):
            SubtitleStyle.model_validate(payload)


class TestResolveSubtitleFont:
    """A font-family id maps to an installed face, else the auto sans face."""

    def test_auto_and_unknown_ids_keep_the_auto_face(self) -> None:
        fallback = _find_cjk_font()
        assert _resolve_subtitle_font(None) == fallback
        assert _resolve_subtitle_font("default") == fallback
        assert _resolve_subtitle_font("DEFAULT") == fallback
        assert _resolve_subtitle_font("") == fallback
        assert _resolve_subtitle_font("comic-sans") == fallback

    def test_a_named_family_prefers_an_installed_face(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        present = tmp_path / "present.ttf"
        present.write_bytes(b"dummy")
        missing = str(tmp_path / "missing.ttf")
        monkeypatch.setitem(
            overlay._SUBTITLE_FONT_FAMILIES,
            "song",
            (missing, str(present)),
        )
        # Neither dummy face hugs the baseline, so the resolver settles on the
        # first candidate that exists instead of the auto face.
        monkeypatch.setattr(
            overlay,
            "_punctuation_hugs_baseline",
            lambda *args, **kwargs: False,
        )
        assert _resolve_subtitle_font("song") == str(present)

    def test_a_named_family_with_no_face_installed_falls_back(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        monkeypatch.setitem(
            overlay._SUBTITLE_FONT_FAMILIES,
            "kai",
            (str(tmp_path / "absent.ttf"),),
        )
        assert _resolve_subtitle_font("kai") == _find_cjk_font()

    def test_a_glob_candidate_matches_on_demand_asset_caches(
        self,
        tmp_path,
        monkeypatch,
    ) -> None:
        # macOS caches on-demand faces (Kaiti…) under opaque asset hashes;
        # the resolver has to expand the glob instead of stat()-ing it.
        cache = tmp_path / "hash.asset" / "AssetData"
        cache.mkdir(parents=True)
        present = cache / "Kaiti.ttc"
        present.write_bytes(b"dummy")
        monkeypatch.setitem(
            overlay._SUBTITLE_FONT_FAMILIES,
            "kai",
            (str(tmp_path / "*" / "AssetData" / "Kaiti.ttc"),),
        )
        monkeypatch.setattr(
            overlay,
            "_punctuation_hugs_baseline",
            lambda *args, **kwargs: False,
        )
        assert _resolve_subtitle_font("kai") == str(present)


class TestSubtitlePlatePainter:
    """The last-resort PNG draws the same block the HTML template does.

    Both read one helper for the user's plate pick, so ``0`` means "no block"
    in the fallback cut as well, and a project that never picked anything gets
    the pack's own alpha byte for byte.
    """

    def test_the_pack_alpha_is_what_an_untouched_project_gets(self) -> None:
        assert _subtitle_pack_painter("warm_note", None)[1] == (
            255,
            248,
            232,
            128,
        )

    def test_a_pick_re_inks_the_pack_block(self) -> None:
        assert _subtitle_pack_painter(
            "warm_note",
            {"background_opacity": 0.25},
        )[1] == (255, 248, 232, 64)

    def test_zero_paints_no_block_at_all(self) -> None:
        assert (
            _subtitle_pack_painter("clean_talk", {"background_opacity": 0})[1]
            is None
        )

    @pytest.mark.parametrize("alpha", [None, 0.0, 0.5, 1.0])
    def test_a_pack_without_a_block_stays_without_one(
        self,
        alpha: float | None,
    ) -> None:
        assert (
            _subtitle_pack_painter(
                "cinematic",
                {"background_opacity": alpha},
            )[1]
            is None
        )


class TestCaptionTemplateFontFamily:
    """An explicit font pick rides into the HTML caption document."""

    def test_a_user_pick_replaces_the_pack_face(self) -> None:
        html = render_caption_template("你好", style={"font_family": "song"})
        assert '"Songti SC"' in html

    def test_no_pick_keeps_the_legacy_family(self) -> None:
        html = render_caption_template("你好")
        assert '"Songti SC"' not in html
        assert '"Noto Sans CJK SC"' in html

    @pytest.mark.parametrize("family", ["default", "comic-sans", ""])
    def test_default_and_unknown_ids_keep_the_legacy_family(
        self,
        family: str,
    ) -> None:
        html = render_caption_template("你好", style={"font_family": family})
        assert '"Songti SC"' not in html


class TestSubtitlePacks:
    def test_every_pack_blueprint_is_registered(self) -> None:
        for pack_id in SUBTITLE_TEMPLATE_PACK_ORDER:
            order = subtitle_pack_caption_order(pack_id)
            assert order, pack_id
            for blueprint in order:
                assert blueprint in CAPTION_BLUEPRINTS

    def test_unknown_or_empty_pack_falls_back_to_none(self) -> None:
        assert subtitle_pack_caption_order(None) is None
        assert subtitle_pack_caption_order("") is None
        assert subtitle_pack_caption_order("nope") is None

    def test_pack_overrides_content_type_order(self) -> None:
        baseline = content_type_caption_order("travel")
        packed = content_type_caption_order("travel", "cinematic")
        assert packed == subtitle_pack_caption_order("cinematic")
        assert packed != baseline

    def test_catalog_text_follows_pack_for_captions(self) -> None:
        text = blueprint_catalog_text(
            "caption",
            subtitle_template="clean_talk",
        )
        assert text.splitlines()[0].startswith("- static_capsule")
        # Decoration catalogs are unaffected by the caption pack.
        decor = blueprint_catalog_text(
            "decoration",
            subtitle_template="clean_talk",
        )
        assert "- static_capsule" not in decor


class TestPreviewSpecs:
    """The style-window cards preview the renderer, so specs == the table."""

    def test_specs_lead_with_smart_card_then_pack_order(self) -> None:
        specs = subtitle_pack_preview_specs()
        assert specs[0]["id"] is None
        assert [s["id"] for s in specs[1:]] == list(
            SUBTITLE_TEMPLATE_PACK_ORDER,
        )

    def test_spec_fields_mirror_pack_table(self) -> None:
        specs = {s["id"]: s for s in subtitle_pack_preview_specs() if s["id"]}
        for pack_id, pack in SUBTITLE_TEMPLATE_PACKS.items():
            spec = specs[pack_id]
            typo = pack["typography"]
            anim = pack["animation"]
            assert spec["plate"] == typo.get("plate", "")
            assert spec["plateRadius"] == typo.get("plateRadius", "0.28em")
            assert spec["glow"] == bool(typo.get("glow", False))
            assert spec["entrance"] == anim["entrance"]
            assert spec["loop"] == anim["loop"]
            assert spec["strokeScale"] == typo["strokeScale"]
            assert spec["letterSpacing"] == typo["letterSpacing"]
            # The UI pastes fontFamily into a style attribute: single
            # quotes only, and the family list itself unchanged.
            family = str(spec["fontFamily"])
            assert '"' not in family
            assert family.replace("'", '"') == typo["fontFamily"]

    def test_every_entrance_is_renderer_known(self) -> None:
        for spec in subtitle_pack_preview_specs():
            assert spec["entrance"] in CAPTION_ENTRANCES

    def test_preview_route_returns_every_pack(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.file_media_routes import router

        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            response = client.get("/subtitle-packs/preview")
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == len(SUBTITLE_TEMPLATE_PACK_ORDER) + 1
        assert items[0]["id"] is None
        assert {i["id"] for i in items[1:]} == set(
            SUBTITLE_TEMPLATE_PACK_ORDER,
        )


@pytest.fixture()
def subtitle_client(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATOR_DATA_ROOT", str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


BUNDLE = {
    "name": "我的综艺字",
    "description": "高饱和描边",
    "packId": "variety_pop",
    "style": {"color": "#ff0000", "fontScale": 1.2},
    "animation": {"entrance": "pop", "loop": True},
}


def test_save_list_update_delete_round_trip(subtitle_client) -> None:
    created = subtitle_client.post("/subtitle-packs/custom", json=BUNDLE)
    assert created.status_code == 200
    item = created.json()
    assert item["styleId"].startswith("user:")
    assert item["packId"] == "variety_pop"
    assert item["style"] == {"color": "#ff0000", "fontScale": 1.2}
    assert item["animation"] == {"entrance": "pop", "loop": True}

    listed = subtitle_client.get("/subtitle-packs/custom")
    assert [i["styleId"] for i in listed.json()["items"]] == [
        item["styleId"],
    ]

    updated = subtitle_client.put(
        f"/subtitle-packs/custom/{item['styleId']}",
        json={**BUNDLE, "name": "改名", "style": {}},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "改名"
    assert updated.json()["style"] == {}

    gone = subtitle_client.delete(f"/subtitle-packs/custom/{item['styleId']}")
    assert gone.status_code == 200
    assert subtitle_client.get("/subtitle-packs/custom").json()["items"] == []


def test_delete_refuses_builtin_and_missing(subtitle_client) -> None:
    refused = subtitle_client.delete("/subtitle-packs/custom/variety_pop")
    assert refused.status_code == 400
    missing = subtitle_client.delete("/subtitle-packs/custom/user:nope")
    assert missing.status_code == 404


def test_update_refuses_unknown_id(subtitle_client) -> None:
    response = subtitle_client.put(
        "/subtitle-packs/custom/user:nope", json=BUNDLE
    )
    assert response.status_code == 404


def test_save_requires_a_name(subtitle_client) -> None:
    response = subtitle_client.post(
        "/subtitle-packs/custom",
        json={**BUNDLE, "name": "  "},
    )
    assert response.status_code == 400


def test_damaged_files_are_skipped(subtitle_client, tmp_path) -> None:
    directory = tmp_path / "user-subtitle-styles"
    directory.mkdir()
    (directory / "user_bad.json").write_text("{not json", encoding="utf-8")
    assert subtitle_client.get("/subtitle-packs/custom").json()["items"] == []


FONT_BUNDLE = {
    "name": "宋体标题",
    "packId": "cinematic",
    "style": {"fontFamily": "song", "color": "#00ff00"},
    "animation": {"entrance": "soft_fade"},
}


def test_the_face_pick_survives_save_list_and_reapply(subtitle_client) -> None:
    """Re-applying a bundle overwrites the draft style; a dropped face would
    silently reset the font the bundle was saved with."""
    created = subtitle_client.post("/subtitle-packs/custom", json=FONT_BUNDLE)
    assert created.status_code == 200
    assert created.json()["style"]["fontFamily"] == "song"

    listed = subtitle_client.get("/subtitle-packs/custom").json()["items"]
    assert listed[0]["style"] == {"color": "#00ff00", "fontFamily": "song"}


def test_update_replaces_or_clears_the_face(subtitle_client) -> None:
    created = subtitle_client.post(
        "/subtitle-packs/custom", json=FONT_BUNDLE
    ).json()
    updated = subtitle_client.put(
        f"/subtitle-packs/custom/{created['styleId']}",
        json={**FONT_BUNDLE, "style": {"fontFamily": "kai"}},
    )
    assert updated.status_code == 200
    assert updated.json()["style"] == {"fontFamily": "kai"}

    cleared = subtitle_client.put(
        f"/subtitle-packs/custom/{created['styleId']}",
        json={**FONT_BUNDLE, "style": {}},
    )
    assert cleared.json()["style"] == {}


def test_legacy_files_without_a_face_still_load(
    subtitle_client, tmp_path
) -> None:
    directory = tmp_path / "user-subtitle-styles"
    directory.mkdir()
    (directory / "user_old.json").write_text(
        json.dumps(
            {
                "style_id": "user:old",
                "name": "老风格",
                "color": "#ff0000",
            },
        ),
        encoding="utf-8",
    )
    items = subtitle_client.get("/subtitle-packs/custom").json()["items"]
    assert items[0]["style"] == {"color": "#ff0000"}
