# -*- coding: utf-8 -*-
"""放映服务:FastAPI 内容端点 + 进度端点。

与 demo-server 的三点差异(都是刻意修正):

1. **无鉴权**。不建 ``users`` / ``tokens``,不校验 Bearer;``user_id`` 由
   :class:`~ivb_player.state.store.ProgressStore` 固定为 1。
2. **分段支持 HTTP Range**。浏览器 ``<video>`` 拖动进度条要发 Range 请求,
   整文件 ``FileResponse`` 会让拖动退化到头部重新下载。
3. **入口来自 ``entry_timeline_id``**,不再靠 ``Object.keys(nodes)[0]`` 的
   JSON 键序巧合。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..format.model import Bundle, is_hex_color
from ..format.reader import (
    BundleError,
    BundleSource,
    Inspection,
    inspect_bundle,
    is_safe_member_name,
    open_source,
)
from ..format.validate import summarize
from ..state.store import ProgressStore

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
DEFAULT_DB_NAME = "state.db"

_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 1 << 20


@dataclass(slots=True)
class BundleService:
    """一个已判定合法的包。每次读内容开一个新 source,避免 zip 句柄跨线程共享。"""

    path: Path
    bundle: Bundle
    inspection: Inspection
    store: ProgressStore

    def source(self) -> BundleSource:
        return open_source(self.path)

    def reload(self) -> "BundleService":
        inspection = inspect_bundle(self.path)
        if inspection.bundle is None:
            raise BundleError(inspection.diagnostics)
        self.inspection = inspection
        self.bundle = inspection.bundle
        return self


class VisitIn(BaseModel):
    timeline_id: str = Field(min_length=1)
    choice_edge: str | None = None
    watched_seconds: float = Field(default=0.0, ge=0)


class ChoiceIn(BaseModel):
    interaction_source: str = Field(min_length=1)
    edge_ref: str = Field(min_length=1)


class WatchIn(BaseModel):
    timeline_id: str = Field(min_length=1)
    watched_seconds: float = Field(ge=0)


class EndingIn(BaseModel):
    timeline_id: str = Field(min_length=1)


class BundleIdIn(BaseModel):
    bundle_id: str | None = None


def _require_known(bundle: Bundle, timeline_id: str, field: str) -> str:
    if timeline_id not in bundle.nodes:
        raise HTTPException(
            status_code=422,
            detail=f"{field} {timeline_id!r} 不在包的内容层中",
        )
    return timeline_id


def project_for_player(service: BundleService) -> dict[str, Any]:
    """把内容层 + 表现层合并成前端一次拿齐的视图。

    选项的 ``label`` / ``prompt`` / ``tone`` 在这里就 join 好:放映端前端不该
    再去 ``edge_index`` 里查第二次 —— demo 的播放器正是漏了这层 join,
    才需要在 JS 里手写 `manifest.edges[edgeRef]`。
    """

    bundle = service.bundle
    presentation = bundle.presentation
    theme = presentation.theme
    css_vars = dict(theme.as_css_vars())
    if not presentation.present and is_hex_color(bundle.meta.accent):
        # 无 presentation 时 meta.accent 是唯一主色来源。
        from ..format.model import hex_to_rgb_triplet

        css_vars["--ivb-accent"] = bundle.meta.accent
        css_vars["--ivb-accent-rgb"] = hex_to_rgb_triplet(bundle.meta.accent)

    def edge_view(edge_ref: str) -> dict[str, Any]:
        edge = bundle.edges.get(edge_ref)
        if edge is None:  # 校验阶段就会拦下,这里只防手工改包
            return {
                "edge_ref": edge_ref,
                "label": "",
                "prompt": "",
                "tone": None,
                "target_timeline_id": "",
            }
        return {
            "edge_ref": edge.edge_id,
            "label": edge.label,
            "prompt": edge.prompt,
            "tone": edge.tone,
            "target_timeline_id": edge.target_timeline_id,
        }

    interactions: list[dict[str, Any]] = []
    for point in bundle.interactions:
        interactions.append(
            {
                "source_timeline_id": point.source_timeline_id,
                "at_seconds": point.at_seconds,
                "question": point.question,
                "countdown_seconds": point.countdown_seconds,
                "default_edge_ref": point.default_edge_ref,
                "options": [
                    {**edge_view(option.edge_ref), "hotspot": option.hotspot}
                    for option in point.options
                ],
            },
        )

    nodes: dict[str, Any] = {}
    for timeline_id, node in bundle.nodes.items():
        nodes[timeline_id] = {
            "timeline_id": timeline_id,
            "title": node.display_title,
            "synopsis": node.synopsis,
            "children": list(node.children),
            "is_ending": node.is_ending,
            "segment": bundle.segments.get(timeline_id, ""),
            "duration": service.inspection.durations.get(timeline_id),
        }

    badge_labels = {
        tone: presentation.badge_label(tone)
        for tone in ("safe", "risky", "danger")
    }
    return {
        "bundle_id": bundle.bundle_id,
        "schema_version": bundle.schema_version,
        "meta": {
            "bundle_id": bundle.meta.bundle_id,
            "title": bundle.meta.title,
            "tagline": bundle.meta.tagline,
            "synopsis": bundle.meta.synopsis,
            "accent": (
                theme.accent if presentation.present else bundle.meta.accent
            ),
        },
        "entry_timeline_id": bundle.entry_timeline_id,
        "nodes": nodes,
        "edges": {edge_id: edge_view(edge_id) for edge_id in bundle.edges},
        "interactions": interactions,
        "theme_css_vars": css_vars,
        "badge_labels": badge_labels,
        "screens": presentation.screens,
        "stylesheets": [
            f"/api/bundle/styles/{name.rsplit('/', 1)[-1]}"
            for name in presentation.stylesheets
        ],
        "totals": {
            "nodes": len(bundle.nodes),
            "endings": len(bundle.endings),
            "interactions": len(bundle.interactions),
        },
    }


def _iter_member(
    path: Path,
    name: str,
    start: int,
    end: int,
) -> Iterator[bytes]:
    """流式读出包内一个条目的 ``[start, end]`` 闭区间。

    自带一个临时 source 并在生成器结束时关闭 —— StreamingResponse 生命周期比
    请求函数长,不能拿请求内的句柄。
    """

    source = open_source(path)
    try:
        yield from source.stream(name, start, end)
    finally:
        source.close()


def _media_response(
    service: BundleService,
    name: str,
    request: Request,
    media_type: str,
) -> Response:
    source = service.source()
    try:
        members = source.names()
        if name not in members:
            raise HTTPException(status_code=404, detail=f"包内没有 {name!r}")
        total = source.size(name) or 0
    finally:
        source.close()

    header = request.headers.get("range")
    start, end = 0, max(total - 1, 0)
    partial = False
    if header:
        match = _RANGE.match(header.strip())
        if match is None:
            raise HTTPException(status_code=416, detail="只支持 bytes= 区间")
        raw_start, raw_end = match.groups()
        if raw_start == "" and raw_end:
            start = max(total - int(raw_end), 0)
        else:
            start = int(raw_start or 0)
            end = int(raw_end) if raw_end else total - 1
        partial = True
    if total == 0 or start > end or start >= total or end >= total:
        raise HTTPException(
            status_code=416,
            headers={"Content-Range": f"bytes */{total}"},
        )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return StreamingResponse(
        _iter_member(service.path, name, start, end),
        status_code=206 if partial else 200,
        media_type=media_type,
        headers=headers,
    )


def create_app(
    bundle_path: str | Path,
    *,
    db_path: str | Path | None = None,
) -> FastAPI:
    """按包路径建放映应用。包不合法直接抛 :class:`BundleError`。"""

    path = Path(bundle_path).expanduser().resolve()
    inspection = inspect_bundle(path)
    if inspection.bundle is None:
        raise BundleError(inspection.diagnostics)
    store = ProgressStore(
        db_path or path.parent / DEFAULT_DB_NAME,
    )
    service = BundleService(
        path=path,
        bundle=inspection.bundle,
        inspection=inspection,
        store=store,
    )

    app = FastAPI(title="IVB Player", version="0.1.0")
    app.state.service = service

    # -- 页面 -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            raise HTTPException(
                status_code=500, detail="缺少 static/index.html"
            )
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.get("/assets/{asset_path:path}")
    def static_asset(asset_path: str) -> Response:
        if not is_safe_member_name(asset_path):
            raise HTTPException(status_code=400, detail="非法静态路径")
        candidate = (STATIC_DIR / asset_path).resolve()
        if STATIC_DIR.resolve() not in candidate.parents:
            raise HTTPException(status_code=403, detail="越界访问")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail=asset_path)
        media = (
            "application/javascript"
            if candidate.suffix == ".js"
            else ("text/css" if candidate.suffix == ".css" else "text/plain")
        )
        return Response(candidate.read_bytes(), media_type=media)

    # -- 内容端点 ---------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "bundle_id": service.bundle.bundle_id,
            "source": service.path.name,
            "warnings": len(service.inspection.warnings),
        }

    @app.get("/api/bundle")
    def manifest() -> dict[str, Any]:
        return project_for_player(service)

    @app.get("/api/validate")
    def validate() -> dict[str, Any]:
        fresh = inspect_bundle(service.path)
        report = fresh.as_report()
        report["summary"] = summarize(fresh.diagnostics)
        return report

    @app.get("/api/bundle/styles/{style_name}")
    def stylesheet(style_name: str) -> Response:
        relative = f"styles/{style_name}"
        if not is_safe_member_name(relative):
            raise HTTPException(status_code=400, detail="非法样式路径")
        source = service.source()
        try:
            if relative not in source.names():
                raise HTTPException(
                    status_code=404, detail=f"包内没有 {relative!r}"
                )
            return Response(source.read_text(relative), media_type="text/css")
        finally:
            source.close()

    @app.get("/api/bundle/segments/{name}")
    def segment(name: str, request: Request) -> Response:
        if not is_safe_member_name(name):
            raise HTTPException(status_code=400, detail="非法分段路径")
        mapped = next(
            (
                path
                for path in service.bundle.segments.values()
                if path.rsplit("/", 1)[-1] == name
            ),
            None,
        )
        if mapped is None:
            raise HTTPException(status_code=404, detail=f"未知分段 {name!r}")
        return _media_response(service, mapped, request, "video/mp4")

    # -- 进度端点 ---------------------------------------------------------

    @app.get("/api/state/progress")
    def progress(
        bundle_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        key = bundle_id or service.bundle.bundle_id
        payload = service.store.progress(key).as_dict()
        payload["total_nodes"] = len(service.bundle.nodes)
        payload["total_endings"] = len(service.bundle.endings)
        payload["path"] = service.store.trail(key)
        return payload

    @app.post("/api/state/visit")
    def visit(body: VisitIn) -> dict[str, str]:
        _require_known(service.bundle, body.timeline_id, "timeline_id")
        service.store.record_visit(
            service.bundle.bundle_id,
            body.timeline_id,
            choice_edge=body.choice_edge,
            watched_seconds=body.watched_seconds,
        )
        return {"ok": "recorded"}

    @app.post("/api/state/watch")
    def watch(body: WatchIn) -> dict[str, Any]:
        """离开节点时回填真实观看秒数。"""

        _require_known(service.bundle, body.timeline_id, "timeline_id")
        updated = service.store.commit_watch_time(
            service.bundle.bundle_id,
            body.timeline_id,
            body.watched_seconds,
        )
        return {"ok": "committed", "rows_updated": updated}

    @app.post("/api/state/choice")
    def choice(body: ChoiceIn) -> dict[str, Any]:
        _require_known(
            service.bundle, body.interaction_source, "interaction_source"
        )
        if body.edge_ref not in service.bundle.edges:
            raise HTTPException(
                status_code=422,
                detail=f"未知边 {body.edge_ref!r}",
            )
        service.store.record_choice(
            service.bundle.bundle_id,
            body.interaction_source,
            body.edge_ref,
        )
        # 目标节点的 visit 由播放器实际进入时再记,不在此提前写,
        # 否则一次选择会在 visits 里留下两行。
        target = service.bundle.edges[body.edge_ref].target_timeline_id
        return {"ok": "recorded", "target_timeline_id": target}

    @app.post("/api/state/ending")
    def ending(body: EndingIn) -> dict[str, Any]:
        _require_known(service.bundle, body.timeline_id, "timeline_id")
        node = service.bundle.nodes[body.timeline_id]
        if not node.is_ending:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{body.timeline_id} 不是结局节点"
                    f"(children={list(node.children)})"
                ),
            )
        first_time = service.store.unlock_ending(
            service.bundle.bundle_id,
            body.timeline_id,
        )
        return {
            "ok": "unlocked",
            "first_time": first_time,
            "endings": service.store.endings(service.bundle.bundle_id),
        }

    @app.get("/api/state/stats")
    def stats(bundle_id: str | None = Query(default=None)) -> dict[str, Any]:
        key = bundle_id or service.bundle.bundle_id
        payload = service.store.stats(key)
        payload["total_nodes"] = len(service.bundle.nodes)
        payload["total_endings"] = len(service.bundle.endings)
        payload["coverage"] = (
            round(payload["distinct_nodes"] / len(service.bundle.nodes), 4)
            if service.bundle.nodes
            else 0.0
        )
        payload["choices"] = service.store.choice_stats(key)
        return payload

    @app.post("/api/state/reset")
    def reset(body: BundleIdIn | None = None) -> dict[str, Any]:
        key = (body.bundle_id if body else None) or service.bundle.bundle_id
        deleted = service.store.clear(key)
        return {"ok": "cleared", "deleted": deleted}

    @app.exception_handler(BundleError)
    def bundle_error_handler(_: Request, exc: BundleError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "diagnostics": [d.as_dict() for d in exc.diagnostics],
            },
        )

    return app


__all__ = [
    "BundleService",
    "create_app",
    "project_for_player",
]
