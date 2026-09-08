# -*- coding: utf-8 -*-
"""放映服务:FastAPI 多包路由 + 进度端点。

一个进程服务任意多包(见 docs/service-design.md §3):``create_app(data_dir)``
不再启动期绑死一个包,而是每个 ``/api/projects/{pid}/…`` 请求按 pid 查目录表定位
磁盘包(:class:`~ivb_player.server.library.ProjectLibrary` 缓存 inspect 结果)。

与 demo-server 的三点差异(都是刻意修正):

1. **无鉴权**。不建 ``users`` / ``tokens``,不校验 Bearer;``user_id`` 由上游随
   请求传入,ivb 只拿它做数据隔离(第 4 步接入),缺省落到 anonymous。
2. **分段支持 HTTP Range**。浏览器 ``<video>`` 拖动进度条要发 Range 请求,
   整文件 ``FileResponse`` 会让拖动退化到头部重新下载。
3. **入口来自 ``entry_timeline_id``**,不再靠 ``Object.keys(nodes)[0]`` 的
   JSON 键序巧合。
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
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
from ..state.store import ANONYMOUS_USER_ID, ProgressStore
from .library import ProjectLibrary, ProjectNotFound

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
_CHUNK = 1 << 20


@dataclass(slots=True)
class BundleService:
    """一次请求内使用的"某 pid 的包 + 进度 store"视图。

    重活(:func:`inspect_bundle`)由 :class:`ProjectLibrary` 按 (pid, mtime) 缓存;
    这里只把已解析的包内容与请求维度的 store 组装起来,构造成本极低。每次读内容
    仍开一个新 source,避免 zip 句柄跨线程共享。
    """

    project_id: str
    path: Path
    bundle: Bundle
    inspection: Inspection
    store: ProgressStore

    def source(self) -> BundleSource:
        return open_source(self.path)


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


def current_user(x_user_id: str | None = Header(default=None)) -> str:
    """从网关注入的 ``X-User-Id`` 头取用户标识。

    ivb 不签发/不校验身份(见 docs/service-design.md §1.1):头由上游可信
    注入,这里只当数据隔离键用。缺省(无头/空值)落 anonymous,未接网关
    时仍可放映。
    """

    user = (x_user_id or "").strip()
    return user or ANONYMOUS_USER_ID


def require_user(x_user_id: str | None = Header(default=None)) -> str:
    """上传这类"必须有归属者"的端点用:缺 ``X-User-Id`` 直接 401。

    与 :func:`current_user` 的区别:放映/进度允许匿名(开发期无网关也能玩),
    但上传要落 ``owner_user_id``,匿名上传会污染目录,故拒绝。
    """

    user = (x_user_id or "").strip()
    if not user:
        raise HTTPException(status_code=401, detail="缺少 X-User-Id 头")
    return user


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
    pid = service.project_id
    presentation = bundle.presentation
    theme = presentation.theme
    css_vars = dict(theme.as_css_vars())
    if not presentation.present and is_hex_color(bundle.meta.accent):
        # 无 presentation 时 meta.accent 是唯一主色来源。
        from ..format.model import hex_to_rgb_triplet

        css_vars["--ivb-accent"] = bundle.meta.accent
        css_vars["--ivb-accent-rgb"] = hex_to_rgb_triplet(bundle.meta.accent)

    #: IVB v1 的 `edge_index` 里没有来源字段,边的来源只能从抉择点的
    #: `options[*].edge_ref` 反推。地图要按 (来源, 目标) 精确认边(否则汇流点
    #: 会把两个父节点的边当成同一条),所以这层反推也在服务端 join 好。
    edge_sources: dict[str, str] = {}
    for point in bundle.interactions:
        for option in point.options:
            edge_sources.setdefault(option.edge_ref, point.source_timeline_id)

    def edge_view(edge_ref: str) -> dict[str, Any]:
        edge = bundle.edges.get(edge_ref)
        if edge is None:  # 校验阶段就会拦下,这里只防手工改包
            return {
                "edge_ref": edge_ref,
                "label": "",
                "prompt": "",
                "tone": None,
                "target_timeline_id": "",
                "source_timeline_id": "",
            }
        return {
            "edge_ref": edge.edge_id,
            "label": edge.label,
            "prompt": edge.prompt,
            "tone": edge.tone,
            "target_timeline_id": edge.target_timeline_id,
            "source_timeline_id": edge_sources.get(edge_ref, ""),
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
            f"/api/projects/{pid}/styles/{name.rsplit('/', 1)[-1]}"
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


async def _stage_upload(
    file: UploadFile,
    max_bytes: int | None,
) -> Path | None:
    """把上传流式落到临时文件;超过 ``max_bytes`` 返回 None(调用方回 413)。

    流式而非一次 ``read()``:互动视频包动辄上百 MB,整包进内存会撑爆进程。
    """

    suffix = Path(file.filename or "bundle.zip").suffix or ".zip"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        staged = Path(handle.name)
        total = 0
        while chunk := await file.read(_CHUNK):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                staged.unlink(missing_ok=True)
                return None
            handle.write(chunk)
    return staged


def create_app(
    data_dir: str | Path,
    *,
    db_path: str | Path | None = None,
    root_path: str = "",
    max_upload_bytes: int | None = None,
) -> FastAPI:
    """建多包放映应用。``data_dir`` 是持久卷根(含 ``ivb.db`` 与 ``bundles/``)。

    与旧单包版的差别:进程启动不再绑死一个包;每个 ``/api/projects/{pid}/…``
    请求按 pid 查目录表定位磁盘包(见 :class:`ProjectLibrary`)。``root_path``
    供挂子路径时用(第 6 步前端 ``<base href>`` 依赖它);``max_upload_bytes``
    限制上传包大小(None = 不限)。
    """

    # FastAPI 工厂天然在此集中注册十余个路由,每个 @app.* 都计入语句数,
    # 与函数复杂度无关 —— 拆成 APIRouter 只为凑 R0915 反而更碎,故就地豁免。
    # pylint: disable=too-many-statements
    library = ProjectLibrary(data_dir, db_path=db_path)

    def service_for(
        project_id: str,
        user_id: str = ANONYMOUS_USER_ID,
    ) -> BundleService:
        resolved = library.resolve(project_id)
        return BundleService(
            project_id=project_id,
            path=resolved.path,
            bundle=resolved.bundle,
            inspection=resolved.inspection,
            store=library.store_for(user_id),
        )

    app = FastAPI(title="IVB Player", version="0.2.0", root_path=root_path)
    app.state.library = library

    # -- 页面 -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if not page.exists():
            raise HTTPException(
                status_code=500,
                detail="缺少 static/index.html",
            )
        html = page.read_text(encoding="utf-8")
        # 注入 <base href>:前端资源与 API 前缀都据此解析,挂子路径
        # (root_path)时才不会全 404(见 docs/service-design.md §6.4)。
        prefix = f"{root_path}/" if root_path else "/"
        base = f'<base href="{prefix}"/>'
        return HTMLResponse(html.replace("<head>", f"<head>\n{base}", 1))

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

    # -- 库级 -------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "projects": len(library.list_projects()),
            "data_dir": str(library.data_dir),
        }

    @app.get("/api/projects")
    def list_projects(
        scope: str = "all",
        user_id: str = Depends(current_user),
    ) -> dict[str, Any]:
        """库列表。``scope=mine`` 只看当前用户上传的,``all``(默认)看全部。"""

        owner = user_id if scope == "mine" else None
        return {
            "ok": True,
            "scope": "mine" if owner else "all",
            "user_id": user_id,
            "projects": [
                record.as_dict() for record in library.list_projects(owner)
            ],
        }

    @app.get("/api/projects/{project_id}")
    def project_detail(project_id: str) -> dict[str, Any]:
        record = library.store.get_project(project_id)
        if record is None:
            raise ProjectNotFound(project_id)
        return record.as_dict()

    @app.post("/api/projects", status_code=201)
    async def upload_project(
        file: UploadFile = File(...),
        title: str | None = Form(default=None),
        user_id: str = Depends(require_user),
    ) -> Response:
        """接收 Creator 导出的 zip:校验 → 落盘 → 登记目录。

        契约见 docs/upload-protocol.md:致命诊断 → 422 回显并丢弃;非 zip →
        400;超 ``max_upload_bytes`` → 413;缺 X-User-Id → 401(require_user)。
        """

        staged = await _stage_upload(file, max_upload_bytes)
        if staged is None:
            return JSONResponse(
                status_code=413,
                content={"ok": False, "detail": "上传超过大小限制"},
            )
        try:
            if not zipfile.is_zipfile(staged):
                return JSONResponse(
                    status_code=400,
                    content={"ok": False, "detail": "不是合法的 zip 包"},
                )
            try:
                record = library.install(
                    staged,
                    owner_user_id=user_id,
                    title=title,
                )
            except BundleError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "ok": False,
                        "diagnostics": [
                            item.as_dict() for item in exc.diagnostics
                        ],
                    },
                )
        finally:
            staged.unlink(missing_ok=True)
        return JSONResponse(
            status_code=201,
            content={
                "ok": True,
                "project_id": record.project_id,
                "title": record.title,
                "node_count": record.node_count,
                "ending_count": record.ending_count,
                "interaction_count": record.interaction_count,
            },
        )

    # -- 内容端点(按 pid) -----------------------------------------------

    @app.get("/api/projects/{project_id}/bundle")
    def manifest(project_id: str) -> dict[str, Any]:
        return project_for_player(service_for(project_id))

    @app.get("/api/projects/{project_id}/validate")
    def validate(project_id: str) -> dict[str, Any]:
        fresh = inspect_bundle(service_for(project_id).path)
        report = fresh.as_report()
        report["summary"] = summarize(fresh.diagnostics)
        return report

    @app.get("/api/projects/{project_id}/styles/{style_name}")
    def stylesheet(project_id: str, style_name: str) -> Response:
        relative = f"styles/{style_name}"
        if not is_safe_member_name(relative):
            raise HTTPException(status_code=400, detail="非法样式路径")
        source = service_for(project_id).source()
        try:
            if relative not in source.names():
                raise HTTPException(
                    status_code=404,
                    detail=f"包内没有 {relative!r}",
                )
            return Response(source.read_text(relative), media_type="text/css")
        finally:
            source.close()

    @app.get("/api/projects/{project_id}/segments/{name}")
    def segment(project_id: str, name: str, request: Request) -> Response:
        if not is_safe_member_name(name):
            raise HTTPException(status_code=400, detail="非法分段路径")
        service = service_for(project_id)
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

    # -- 进度端点(按 pid) -----------------------------------------------

    @app.get("/api/projects/{project_id}/state/progress")
    def progress(
        project_id: str,
        user_id: str = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_for(project_id, user_id)
        payload = service.store.progress(project_id).as_dict()
        payload["total_nodes"] = len(service.bundle.nodes)
        payload["total_endings"] = len(service.bundle.endings)
        payload["path"] = service.store.trail(project_id)
        return payload

    @app.post("/api/projects/{project_id}/state/visit")
    def visit(
        project_id: str,
        body: VisitIn,
        user_id: str = Depends(current_user),
    ) -> dict[str, str]:
        service = service_for(project_id, user_id)
        _require_known(service.bundle, body.timeline_id, "timeline_id")
        service.store.record_visit(
            project_id,
            body.timeline_id,
            choice_edge=body.choice_edge,
            watched_seconds=body.watched_seconds,
        )
        return {"ok": "recorded"}

    @app.post("/api/projects/{project_id}/state/watch")
    def watch(
        project_id: str,
        body: WatchIn,
        user_id: str = Depends(current_user),
    ) -> dict[str, Any]:
        """离开节点时回填真实观看秒数。"""

        service = service_for(project_id, user_id)
        _require_known(service.bundle, body.timeline_id, "timeline_id")
        updated = service.store.commit_watch_time(
            project_id,
            body.timeline_id,
            body.watched_seconds,
        )
        return {"ok": "committed", "rows_updated": updated}

    @app.post("/api/projects/{project_id}/state/choice")
    def choice(
        project_id: str,
        body: ChoiceIn,
        user_id: str = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_for(project_id, user_id)
        _require_known(
            service.bundle,
            body.interaction_source,
            "interaction_source",
        )
        if body.edge_ref not in service.bundle.edges:
            raise HTTPException(
                status_code=422,
                detail=f"未知边 {body.edge_ref!r}",
            )
        service.store.record_choice(
            project_id,
            body.interaction_source,
            body.edge_ref,
        )
        # 目标节点的 visit 由播放器实际进入时再记,不在此提前写,
        # 否则一次选择会在 visits 里留下两行。
        target = service.bundle.edges[body.edge_ref].target_timeline_id
        return {"ok": "recorded", "target_timeline_id": target}

    @app.post("/api/projects/{project_id}/state/ending")
    def ending(
        project_id: str,
        body: EndingIn,
        user_id: str = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_for(project_id, user_id)
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
            project_id,
            body.timeline_id,
        )
        return {
            "ok": "unlocked",
            "first_time": first_time,
            "endings": service.store.endings(project_id),
        }

    @app.get("/api/projects/{project_id}/state/stats")
    def stats(
        project_id: str,
        user_id: str = Depends(current_user),
    ) -> dict[str, Any]:
        service = service_for(project_id, user_id)
        payload: dict[str, Any] = service.store.stats(project_id)
        payload["total_nodes"] = len(service.bundle.nodes)
        payload["total_endings"] = len(service.bundle.endings)
        payload["coverage"] = (
            round(payload["distinct_nodes"] / len(service.bundle.nodes), 4)
            if service.bundle.nodes
            else 0.0
        )
        payload["choices"] = service.store.choice_stats(project_id)
        return payload

    @app.post("/api/projects/{project_id}/state/reset")
    def reset(
        project_id: str,
        user_id: str = Depends(current_user),
    ) -> dict[str, Any]:
        deleted = service_for(project_id, user_id).store.clear(project_id)
        return {"ok": "cleared", "deleted": deleted}

    # -- 异常处理 ---------------------------------------------------------

    @app.exception_handler(ProjectNotFound)
    def project_not_found(_: Request, exc: ProjectNotFound) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "detail": f"未登记的项目 {exc.project_id!r}",
            },
        )

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
