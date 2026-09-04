# -*- coding: utf-8 -*-
"""``ivb`` 命令行。

::

    ivb validate <包路径> [--json]     只跑校验,退出码反映结论
    ivb info     <包路径>              故事树摘要
    ivb serve    <包路径> [--port]     起放映服务
    ivb demo     [--out demo.zip]      产一个免 Creator 的开箱包
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python ivb_player/cli.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ivb_player.format.errors import Severity  # noqa: E402
from ivb_player.format.reader import (  # noqa: E402
    BundleError,
    Inspection,
    inspect_bundle,
)
from ivb_player.format.validate import summarize  # noqa: E402
from ivb_player.testing import write_demo_bundle  # noqa: E402


def _load(path: str) -> Inspection:
    return inspect_bundle(Path(path))


def _print_diagnostics(inspection: Inspection, *, verbose: bool) -> None:
    for diagnostic in inspection.diagnostics:
        marker = "✗" if diagnostic.is_fatal else "·"
        if (
            verbose
            or diagnostic.is_fatal
            or diagnostic.severity is Severity.WARNING
        ):
            print(f"  {marker} {diagnostic}")


def cmd_validate(args: argparse.Namespace) -> int:
    inspection = _load(args.bundle)
    summary = summarize(inspection.diagnostics)
    if args.json:
        report = inspection.as_report()
        report["summary"] = summary
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        state = "OK" if inspection.bundle is not None else "INVALID"
        print(f"{state}  {inspection.source_label}")
        if inspection.bundle is not None:
            bundle = inspection.bundle
            print(
                f"  节点 {len(bundle.nodes)} / 边 {len(bundle.edges)} / "
                f"抉择点 {len(bundle.interactions)} / 结局 {len(bundle.endings)}",
            )
        _print_diagnostics(inspection, verbose=args.verbose)
        print(
            f"  致命 {summary['fatal']} · 告警 {summary['warning']}",
        )
    return 1 if summary["fatal"] else 0


def cmd_info(args: argparse.Namespace) -> int:
    inspection = _load(args.bundle)
    bundle = inspection.bundle
    if bundle is None:
        print("包不可放映,先跑 `ivb validate` 看诊断:", file=sys.stderr)
        _print_diagnostics(inspection, verbose=True)
        return 1
    print(f"{bundle.meta.title}  ({bundle.bundle_id})")
    print(f"  入口 {bundle.entry_timeline_id} · 结局 {len(bundle.endings)} 个")
    covered = {point.source_timeline_id for point in bundle.interactions}
    for timeline_id in bundle.nodes:
        node = bundle.nodes[timeline_id]
        flags = []
        if timeline_id in covered:
            flags.append("抉择")
        if node.is_ending:
            flags.append("结局")
        turn = "→ " + " | ".join(node.children) if node.children else "◇"
        print(
            f"  [{','.join(flags) or '过场'}] {timeline_id:<28}"
            f" {node.display_title:<16} {turn}",
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from ivb_player.server.app import create_app

    try:
        app = create_app(args.bundle, db_path=args.db)
    except BundleError as exc:
        print("包不可放映:", file=sys.stderr)
        for diagnostic in exc.diagnostics:
            print(f"  {diagnostic}", file=sys.stderr)
        return 1
    service = app.state.service
    bundle = service.bundle
    print(f"放映 {bundle.meta.title} ·  {bundle.bundle_id}")
    print(f"  包   {service.path}")
    print(f"  状态 {service.store.db_path}")
    print(f"  http://127.0.0.1:{args.port}/")
    if service.inspection.warnings:
        print(
            f"  {len(service.inspection.warnings)} 条告警,`ivb validate` 查看"
        )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser().resolve()
    write_demo_bundle(out)
    print(f"已生成开箱包 {out}")
    if args.serve:
        return cmd_serve(
            argparse.Namespace(
                bundle=str(out),
                host=args.host,
                port=args.port,
                db=args.db,
            ),
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ivb",
        description="IVB 互动视频包放映端",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler, help_text in (
        ("validate", cmd_validate, "校验包结构并输出诊断"),
        ("info", cmd_info, "输出故事树摘要"),
    ):
        spin = sub.add_parser(name, help=help_text)
        spin.add_argument("bundle", help="zip 或解包后的目录")
        spin.add_argument("--verbose", "-v", action="store_true")
        if name == "validate":
            spin.add_argument(
                "--json", action="store_true", help="输出机器可读报告"
            )
        spin.set_defaults(func=handler)

    serve = sub.add_parser("serve", help="起本地放映服务")
    serve.add_argument("bundle")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument(
        "--db", default=None, help="state.db 路径,默认与包同目录"
    )
    serve.set_defaults(func=cmd_serve)

    demo = sub.add_parser("demo", help="产一个免 Creator 的开箱包")
    demo.add_argument("--out", default="demo.ivb.zip")
    demo.add_argument("--serve", action="store_true", help="生成后直接起服务")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8766)
    demo.add_argument("--db", default=None)
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
