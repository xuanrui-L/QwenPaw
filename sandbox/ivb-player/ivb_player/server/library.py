# -*- coding: utf-8 -*-
"""多包库:把 ``data_dir`` 管理成"目录表 + ``bundles/{pid}/``"的放映库。

单进程服务任意多包(见 docs/service-design.md §3、§6):

- :meth:`ProjectLibrary.install` 校验一个 Creator 导出包(zip 或目录),落盘到
  ``bundles/{project_id}/`` 并登记进 ``projects`` 目录表。第 5 步的 HTTP 上传
  端点、启动脚本与测试都复用它把包塞进库。
- :meth:`ProjectLibrary.resolve` 按 ``project_id`` 查目录 → 定位磁盘包 →
  ``inspect_bundle``,结果按 ``(pid, mtime)`` 缓存,避免每请求重读包。

本模块不含 SQL(那是 :class:`~ivb_player.state.store.ProgressStore` 的事),也
不碰 HTTP(那是 :mod:`ivb_player.server.app` 的事)—— 只管"包 ↔ 目录表 ↔ 缓存"。
"""

from __future__ import annotations

import shutil
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..format import errors
from ..format.model import Bundle
from ..format.reader import (
    BundleError,
    Inspection,
    inspect_bundle,
    is_safe_member_name,
)
from ..state.store import ANONYMOUS_USER_ID, ProgressStore, ProjectRecord

#: ``data_dir`` 下的固定布局:单库文件 + 每包一个子目录(见 §4、§7.1)。
DB_NAME = "ivb.db"
BUNDLES_DIRNAME = "bundles"


class ProjectNotFound(KeyError):
    """目录表里没有这个 ``project_id``(没上传过,或拼错)。"""

    def __init__(self, project_id: str) -> None:
        super().__init__(project_id)
        self.project_id = project_id


@dataclass(frozen=True, slots=True)
class ResolvedProject:
    """一次解析的产物:磁盘路径 + 已判定合法的 inspection。"""

    project_id: str
    path: Path
    inspection: Inspection

    @property
    def bundle(self) -> Bundle:
        """install/resolve 已保证合法,这里免去上层反复判空。"""

        bundle = self.inspection.bundle
        if bundle is None:  # pragma: no cover - 不变式兜底
            raise BundleError(self.inspection.diagnostics)
        return bundle


class ProjectLibrary:
    """``data_dir`` 的多包视图:安装、按 pid 解析(带缓存)、目录列举。"""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        db_path: str | Path | None = None,
        user_id: str = ANONYMOUS_USER_ID,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = (
            Path(db_path).expanduser()
            if db_path is not None
            else self.data_dir / DB_NAME
        )
        #: catalog(install/resolve/list)与缺省进度都走这个绑缺省用户的
        #: store;具体用户的进度视图由 :meth:`store_for` 按需派生并缓存。
        self.store = ProgressStore(self.db_path, user_id=user_id)
        self._stores: dict[str, ProgressStore] = {user_id: self.store}
        self._cache: dict[str, tuple[float, Inspection]] = {}
        self._lock = threading.Lock()

    # -- 用户进度视图 --------------------------------------------------

    def store_for(self, user_id: str) -> ProgressStore:
        """按 ``user_id`` 取进度 store(缓存复用)。

        进度方法绑 ``store.user_id``,故每个上游用户一个视图;(user_id, pid)
        复合隔离由 store 内部 SQL 保证(见 docs/service-design.md §4.2)。
        catalog 方法与用户无关,仍走 :attr:`store`。
        """

        with self._lock:
            cached = self._stores.get(user_id)
            if cached is None:
                cached = ProgressStore(self.db_path, user_id=user_id)
                self._stores[user_id] = cached
            return cached

    # -- 安装(落盘 + 登记目录) ------------------------------------------

    def install(
        self,
        source: str | Path,
        *,
        owner_user_id: str,
        title: str | None = None,
    ) -> ProjectRecord:
        """校验并收编一个包。致命诊断 → 抛 :class:`BundleError`,不落盘不登记。

        ``owner_user_id`` 记首次上传者;重复安装同 pid 覆盖文件、更新目录行,但
        owner 与 created_at 不变(见 service-design §1.5)。
        """

        origin = Path(source).expanduser().resolve()
        inspection = inspect_bundle(origin)
        if inspection.bundle is None:
            raise BundleError(inspection.diagnostics)
        bundle = inspection.bundle
        project_id = bundle.meta.bundle_id
        if not is_safe_member_name(project_id):
            # pid 会被当目录名,含 .. / 绝对路径就会写出 data_dir 之外。
            raise BundleError(
                [
                    errors.make(
                        "PATH_ESCAPE",
                        "meta.bundle_id",
                        value=project_id,
                    ),
                ],
            )
        dest = self.data_dir / BUNDLES_DIRNAME / project_id
        _materialize(origin, dest)
        record = ProjectRecord(
            project_id=project_id,
            owner_user_id=owner_user_id,
            title=title or bundle.meta.title,
            synopsis=bundle.meta.synopsis,
            node_count=len(bundle.nodes),
            ending_count=len(bundle.endings),
            interaction_count=len(bundle.interactions),
            storage_path=f"{BUNDLES_DIRNAME}/{project_id}",
        )
        self.store.upsert_project(record)
        with self._lock:
            self._cache.pop(project_id, None)  # 重新安装立即让旧缓存失效
        # 回读一行:拿到 DB 填好的 created_at/updated_at(见 ProjectRecord 文档)。
        return self.store.get_project(project_id) or record

    # -- 解析(放映用,带缓存) -------------------------------------------

    def resolve(self, project_id: str) -> ResolvedProject:
        """目录表查 pid → 磁盘包 → inspection;命中 ``(pid, mtime)`` 缓存不重读。"""

        record = self.store.get_project(project_id)
        if record is None:
            raise ProjectNotFound(project_id)
        path = self.data_dir / record.storage_path
        stamp = _mtime(path)
        with self._lock:
            hit = self._cache.get(project_id)
            if hit is not None and hit[0] == stamp:
                return ResolvedProject(project_id, path, hit[1])
        inspection = inspect_bundle(path)
        if inspection.bundle is None:
            raise BundleError(inspection.diagnostics)
        with self._lock:
            self._cache[project_id] = (stamp, inspection)
        return ResolvedProject(project_id, path, inspection)

    # -- 目录列举 --------------------------------------------------------

    def list_projects(
        self,
        owner_user_id: str | None = None,
    ) -> list[ProjectRecord]:
        """``owner_user_id`` 为 None 返回全部,否则只返回该用户的(库页"我的")。"""

        return self.store.list_projects(owner_user_id)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # pragma: no cover - 目录被外部删掉的兜底
        return 0.0


def _materialize(source: Path, dest: Path) -> None:
    """把 zip 或目录落成 ``dest`` 目录;重复安装先清后写(见 §1.5)。"""

    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, dest)
    elif zipfile.is_zipfile(source):
        _safe_unzip(source, dest)
    else:  # pragma: no cover - install 前 inspect_bundle 已拦下不可读源
        raise BundleError([errors.make("BUNDLE_UNREADABLE", str(source))])


def _safe_unzip(archive_path: Path, dest: Path) -> None:
    """解压前逐个成员名过 :func:`is_safe_member_name`,堵 zip slip。"""

    with zipfile.ZipFile(archive_path) as archive:
        for name in archive.namelist():
            if not is_safe_member_name(name):
                raise BundleError(
                    [
                        errors.make(
                            "PATH_ESCAPE",
                            archive_path.name,
                            value=name,
                        ),
                    ],
                )
        archive.extractall(dest)


__all__ = [
    "BUNDLES_DIRNAME",
    "DB_NAME",
    "ProjectLibrary",
    "ProjectNotFound",
    "ResolvedProject",
]
