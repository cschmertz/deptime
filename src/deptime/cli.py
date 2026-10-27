"""`deptime` command line.

Everyday use (local, private):
  deptime mcp                     run the MCP server over stdio for Claude Code / Cursor / Codex / VS Code
  deptime explain|history|check|diff|coverage
  deptime index pull|status       manage the prebuilt index (downloaded from GitHub Releases)
Building the index yourself / CI:
  deptime ingest <pkg>|--all      analyse PyPI releases into a locally built index
  deptime index pack --out DIR    produce index.sqlite.gz + manifest.json for publishing
Hosting (optional):
  deptime serve                   MCP over HTTP at /mcp plus a JSON API at /v1/*
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from deptime.config import Settings, load_coverage
from deptime.index.store import IndexStore
from deptime.models import SymbolQuery
from deptime.query import DepTime


def _err(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _dt(settings: Settings) -> DepTime:
    """Query-command setup: download the index on first use (synchronously), then open it read-only."""
    if not settings.index_path.exists():
        from deptime.index.distribution import ensure_index

        if not settings.offline:
            _err(f"deptime: no index at {settings.index_path}; downloading it once…")
        _err(f"deptime: {ensure_index(settings).message}")
    store = IndexStore.open(settings.index_path) if settings.index_path.exists() else None
    return DepTime(store, load_coverage(settings.coverage_file), settings.index_path)


def _print(model) -> None:
    print(model.model_dump_json(indent=2, exclude_none=True))


def cmd_ingest(args: argparse.Namespace, settings: Settings) -> int:
    from deptime.index.distribution import mark_locally_built, read_local_meta
    from deptime.ingest.pipeline import ingest_package

    coverage = load_coverage(settings.coverage_file)
    names = [p.name for p in coverage.packages] if args.all else args.packages
    if not names:
        _err("name at least one package, or pass --all")
        return 2
    if read_local_meta(settings.index_path) is not None and not args.into_downloaded:
        _err(
            f"{settings.index_path} is the downloaded index; ingesting into it would stop its daily updates.\n"
            "Build your own elsewhere (e.g. DEPTIME_HOME=./.deptime deptime ingest ...), "
            "or pass --into-downloaded to turn this one into a locally built index."
        )
        return 2
    store = IndexStore.open(settings.index_path, readonly=False)
    marked = False
    failures = 0
    for name in names:
        spec = coverage.get(name)
        if spec is None:
            _err(f"{name}: not in the coverage list (src/deptime/coverage.toml or $DEPTIME_COVERAGE_FILE)")
            failures += 1
            continue
        try:
            report = ingest_package(
                spec, coverage.policy, store, settings.cache_dir, versions=args.versions or None, log=_err
            )
        except Exception as exc:  # one broken package must not stop the rest of the run
            _err(f"[{name}] FAILED: {type(exc).__name__}: {exc}")
            failures += 1
            continue
        if report.stats is not None and not marked:
            mark_locally_built(
                settings.index_path
            )  # from now on this index is yours; it is never auto-replaced
            marked = True
        failures += bool(report.failed)
        if report.failed:
            _err(json.dumps({"package": name, "failed": report.failed}, indent=2))
    store.close()
    return 1 if failures else 0


def cmd_explain(args: argparse.Namespace, settings: Settings) -> int:
    text = Path(args.file).read_text() if args.file else sys.stdin.read()
    installed = dict(item.split("==", 1) for item in args.installed)
    freeze = Path(args.freeze).read_text() if args.freeze else None
    _print(_dt(settings).explain_error(text, installed or None, freeze))
    return 0


def cmd_history(args: argparse.Namespace, settings: Settings) -> int:
    _print(_dt(settings).symbol_history(args.package, args.path))
    return 0


def cmd_check(args: argparse.Namespace, settings: Settings) -> int:
    queries = []
    for raw in args.symbols:
        path, _, kw = raw.partition(":")
        queries.append(SymbolQuery(path=path, kwargs=[k for k in kw.split(",") if k]))
    _print(_dt(settings).check_symbols(args.package, args.version, queries))
    return 0


def cmd_diff(args: argparse.Namespace, settings: Settings) -> int:
    _print(
        _dt(settings).diff_versions(
            args.package,
            args.from_version,
            args.to_version,
            args.used or None,
            args.include_added,
            args.limit,
        )
    )
    return 0


def cmd_coverage(args: argparse.Namespace, settings: Settings) -> int:
    _print(_dt(settings).coverage_report())
    return 0


def cmd_index(args: argparse.Namespace, settings: Settings) -> int:
    from deptime.index import distribution as dist

    if args.action == "pull":
        local = dist.read_local_meta(settings.index_path)
        if settings.index_path.exists() and local is None and not args.force:
            _err(
                f"{settings.index_path} was built locally; pass --force to replace it with the published index"
            )
            return 1
        with dist.http_client() as client:
            manifest = dist.fetch_manifest(settings.index_url, client)
            if local and local.get("manifest", {}).get("sha256") == manifest.sha256 and not args.force:
                _err(f"deptime: index already up to date (built {manifest.built_at})")
                return 0
            dist.download(settings.index_url, settings.index_path, client, manifest=manifest)
        _err(f"deptime: installed index with {manifest.packages} packages, built {manifest.built_at}")
        return 0

    if args.action == "pack":
        if not settings.index_path.exists():
            _err(f"no index at {settings.index_path}; run `deptime ingest` first")
            return 1
        manifest = dist.pack(settings.index_path, Path(args.out))
        print(json.dumps(manifest.__dict__, indent=2))
        return 0

    # status
    local = dist.read_local_meta(settings.index_path)
    status: dict[str, object] = {
        "index_path": str(settings.index_path),
        "exists": settings.index_path.exists(),
        "source": "downloaded" if local else ("locally built" if settings.index_path.exists() else None),
        "index_url": settings.index_url,
        "offline": settings.offline,
    }
    if settings.index_path.exists():
        store = IndexStore.open(settings.index_path)
        status.update(
            built_at=store.built_at(),
            packages=len(store.package_rows()),
            size_bytes=settings.index_path.stat().st_size,
        )
        store.close()
    if local:
        status.update(downloaded_at=local.get("downloaded_at"), checked_at=local.get("checked_at"))
    print(json.dumps(status, indent=2))
    return 0


def cmd_mcp(args: argparse.Namespace, settings: Settings) -> int:
    """Local stdio MCP server. stdout is the protocol channel: everything else goes to stderr."""
    from deptime.index.distribution import start_background_refresh
    from deptime.server.mcp_app import build_mcp

    store = IndexStore.open(settings.index_path) if settings.index_path.exists() else None
    dt = DepTime(store, load_coverage(settings.coverage_file), settings.index_path)
    start_background_refresh(dt, settings)
    build_mcp(dt).run(transport="stdio")
    return 0


def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from deptime.server.http_app import create_app

    uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deptime", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("mcp", help="run the MCP server over stdio (local mode)")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("explain", help="explain a traceback (stdin or --file)")
    p.add_argument("--file")
    p.add_argument("--installed", nargs="*", default=[], metavar="PKG==VER")
    p.add_argument("--freeze", help="path to `pip freeze` output")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("history", help="version history of one symbol")
    p.add_argument("package")
    p.add_argument("path")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("check", help="check symbols (path or path:kw1,kw2) at a version")
    p.add_argument("package")
    p.add_argument("version")
    p.add_argument("symbols", nargs="+")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("diff", help="breaking changes between versions (from may be YYYY-MM-DD)")
    p.add_argument("package")
    p.add_argument("from_version")
    p.add_argument("to_version", nargs="?")
    p.add_argument("--used", nargs="*")
    p.add_argument("--include-added", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("coverage", help="what is indexed")
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("index", help="manage the index: pull | status | pack")
    p.add_argument("action", choices=["pull", "status", "pack"])
    p.add_argument("--force", action="store_true", help="pull: replace a locally built or current index")
    p.add_argument("--out", default="dist", help="pack: output directory")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("ingest", help="build or extend a local index from PyPI releases")
    p.add_argument("packages", nargs="*")
    p.add_argument("--all", action="store_true", help="every package in the coverage list")
    p.add_argument(
        "--versions",
        nargs="*",
        help="add these versions instead of the policy's selection (indexed ones are kept)",
    )
    p.add_argument(
        "--into-downloaded",
        action="store_true",
        help="allow ingesting into a downloaded index (it then stops receiving daily updates)",
    )
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("serve", help="host MCP over HTTP (/mcp) plus the JSON API (/v1/*)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args, Settings.from_env())


if __name__ == "__main__":
    raise SystemExit(main())
