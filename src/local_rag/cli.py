import argparse
import json
from pathlib import Path
from typing import Any, Optional, Sequence

from .config import Settings, default_home, parse_exclusions
from .service import LocalRAG


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="local-rag")
    result.add_argument(
        "--home", type=Path, default=default_home(), help="data root (default ~/.local-rag)"
    )
    commands = result.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="initialize exactly one recursive source root")
    initialize.add_argument("root", type=Path)
    initialize.add_argument("--exclude", action="append", default=[])
    initialize.add_argument("--reconcile-seconds", type=float, default=60)

    for name in ("scan", "reconcile"):
        command = commands.add_parser(name, help="reconcile files with the index")
        command.add_argument("target", nargs="?", help="relative file/folder; omitted means all")

    reindex = commands.add_parser(
        "reindex", aliases=["rebuild"], help="force rebuild by target or all"
    )
    group = reindex.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--target", help="relative file or folder")

    search = commands.add_parser("search", help="global or subfolder-scoped search")
    search.add_argument("query")
    search.add_argument("--scope")
    search.add_argument("--limit", type=int, default=8)

    read = commands.add_parser("read", help="read indexed extracted text")
    read.add_argument("path")
    read.add_argument("--start", type=int, default=0)
    read.add_argument("--length", type=int, default=12000)

    commands.add_parser("status")
    commands.add_parser(
        "serve", aliases=["watch"], help="watch continuously and reconcile periodically"
    )
    commands.add_parser("mcp", help="serve MCP over stdio")

    review = commands.add_parser("review", help="list or resolve durable review items")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_list = review_commands.add_parser("list")
    review_list.add_argument("--status", default="open", choices=["open", "resolved"])
    review_resolve = review_commands.add_parser("resolve")
    review_resolve.add_argument("id", type=int)
    review_resolve.add_argument("resolution")

    metadata = commands.add_parser("metadata", help="read or add evidence-backed metadata")
    metadata_commands = metadata.add_subparsers(dest="metadata_command", required=True)
    metadata_get = metadata_commands.add_parser("get")
    metadata_get.add_argument("path")
    metadata_add = metadata_commands.add_parser("add")
    metadata_add.add_argument("path")
    metadata_add.add_argument("key")
    metadata_add.add_argument("value_json")
    metadata_add.add_argument("evidence_json")
    metadata_add.add_argument("--actor", default="cli")

    relationship = commands.add_parser("relationship", help="add an evidence-backed relationship")
    relationship.add_argument("source")
    relationship.add_argument("target")
    relationship.add_argument("relation")
    relationship.add_argument("evidence_json")
    relationship.add_argument("--actor", default="cli")

    ocr = commands.add_parser("ocr", help="manage the pinned local OCR runtime")
    ocr_commands = ocr.add_subparsers(dest="ocr_command", required=True)
    ocr_commands.add_parser("install")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    home = args.home.expanduser().resolve()
    if args.command == "init":
        root = args.root.expanduser().resolve()
        if not root.is_dir():
            raise SystemExit(f"source root is not a directory: {root}")
        settings = Settings(
            root=root,
            home=home,
            exclusions=parse_exclusions(args.exclude),
            reconcile_seconds=args.reconcile_seconds,
        )
        settings.save()
        service = LocalRAG(settings)
        _print({"initialized": True, **service.status()})
        return 0

    settings = Settings.load(home)
    service = LocalRAG(settings)
    command = args.command
    if command in {"scan", "reconcile"}:
        output = service.scan(args.target)
    elif command in {"reindex", "rebuild"}:
        output = service.scan(None if args.all else args.target, force=True)
    elif command == "search":
        output = service.search(args.query, args.limit, args.scope)
    elif command == "read":
        output = service.read(args.path, args.start, args.length)
    elif command == "status":
        output = service.status()
    elif command in {"serve", "watch"}:
        from .watcher import WatchService

        try:
            WatchService(service).run()
        except KeyboardInterrupt:
            pass
        return 0
    elif command == "mcp":
        from .mcp import serve

        return serve(service)
    elif command == "review":
        output = (
            service.reviews(args.status)
            if args.review_command == "list"
            else service.resolve_review(args.id, args.resolution)
        )
    elif command == "metadata":
        output = (
            service.metadata(args.path)
            if args.metadata_command == "get"
            else service.add_metadata(
                args.path,
                args.key,
                _json(args.value_json, "value_json"),
                _evidence(args.evidence_json),
                args.actor,
            )
        )
    elif command == "relationship":
        output = service.add_relationship(
            args.source, args.target, args.relation, _evidence(args.evidence_json), args.actor
        )
    elif command == "ocr" and args.ocr_command == "install":
        output = service.ocr_runtime.install()
    else:
        raise AssertionError(f"unhandled command: {command}")
    _print(output)
    return 1 if isinstance(output, dict) and output.get("errors") else 0


def _json(value: str, label: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid {label}: {exc}") from exc


def _evidence(value: str) -> Any:
    evidence = _json(value, "evidence_json")
    if not isinstance(evidence, list) or not evidence:
        raise SystemExit("evidence_json must be a non-empty JSON array")
    return evidence


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
