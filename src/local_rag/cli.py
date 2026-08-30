from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from .config import Settings, default_home, parse_exclusions
from .search import SEARCH_MODES
from .service import MultiSourceRAG


def parser(prog: str = "local-rag-mcp") -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog=prog)
    result.add_argument(
        "--home", type=Path, default=default_home(), help="shared data root (default ~/.local-rag)"
    )
    commands = result.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser(
        "init", help="initialize storage and optionally migrate one legacy local root"
    )
    initialize.add_argument("root", type=Path, nargs="?")
    initialize.add_argument("--exclude", action="append", default=[])
    initialize.add_argument("--reconcile-seconds", type=float, default=60)

    source = commands.add_parser("source", help="manage local and Google Drive sources")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_commands.add_parser("list")
    local = source_commands.add_parser("add-local")
    local.add_argument("name")
    local.add_argument("root", type=Path)
    local.add_argument("--exclude", action="append", default=[])
    drive = source_commands.add_parser("add-drive")
    drive.add_argument("name")
    drive.add_argument("root_id")
    drive.add_argument("--account", required=True)
    drive.add_argument("--token-file", type=Path, required=True)
    drive.add_argument("--shared-drive-id", default="")
    drive.add_argument("--exclude", action="append", default=[])
    for name in ("enable", "disable"):
        subcommand = source_commands.add_parser(name)
        subcommand.add_argument("source")
    remove = source_commands.add_parser("remove")
    remove.add_argument("source")
    remove.add_argument("--yes", action="store_true", help="confirm local index/cache purge")

    for name in ("scan", "sync", "reconcile"):
        command = commands.add_parser(name, help="incrementally reconcile enabled sources")
        command.add_argument("target", nargs="?", help="local relative file/folder")
        command.add_argument("--source")
        command.add_argument("--full", action="store_true", help="authoritative Drive tree sync")
    reindex = commands.add_parser(
        "reindex", aliases=["rebuild"], help="rebuild file/folder/source/all index state"
    )
    reindex.add_argument("--source")
    reindex.add_argument("--target", help="relative file or folder within the source")
    reindex.add_argument("--all", action="store_true")
    reindex.add_argument(
        "--reextract", action="store_true", help="deliberately rerun extraction/OCR"
    )

    search = commands.add_parser("search", help="global search with optional source/folder scope")
    search.add_argument("query")
    search.add_argument("--source")
    search.add_argument("--folder", "--scope", dest="folder")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--mode", choices=SEARCH_MODES, default="hybrid")
    read = commands.add_parser("read", help="read one cached indexed artifact")
    read.add_argument("path")
    read.add_argument("--source")
    read.add_argument("--start", type=int, default=0)
    read.add_argument("--length", type=int, default=12000)
    commands.add_parser("status")
    commands.add_parser(
        "serve", aliases=["watch"], help="watch local roots and periodically sync all sources"
    )
    mcp = commands.add_parser("mcp", help="serve MCP SDK over stdio")
    mcp.add_argument(
        "--profile", "--mode", choices=("reader", "reviewer", "admin"), default="reader"
    )

    review = commands.add_parser("review", help="list, resolve, or correct OCR reviews")
    review_commands = review.add_subparsers(dest="review_command", required=True)
    review_list = review_commands.add_parser("list")
    review_list.add_argument("--status", default="open", choices=["open", "resolved"])
    review_resolve = review_commands.add_parser("resolve")
    review_resolve.add_argument("id", type=int)
    review_resolve.add_argument("resolution")
    review_correct = review_commands.add_parser("correct")
    review_correct.add_argument("id", type=int)
    review_correct.add_argument("text")
    review_correct.add_argument("evidence_json")
    review_correct.add_argument("--actor", required=True)
    metadata = commands.add_parser("metadata", help="read or add evidence-backed metadata")
    metadata_commands = metadata.add_subparsers(dest="metadata_command", required=True)
    metadata_get = metadata_commands.add_parser("get")
    metadata_get.add_argument("path")
    metadata_get.add_argument("--source")
    metadata_add = metadata_commands.add_parser("add")
    metadata_add.add_argument("path")
    metadata_add.add_argument("key")
    metadata_add.add_argument("value_json")
    metadata_add.add_argument("evidence_json")
    metadata_add.add_argument("--source")
    metadata_add.add_argument("--actor", default="cli")
    relationship = commands.add_parser("relationship", help="add an evidence-backed relationship")
    relationship.add_argument("source_path")
    relationship.add_argument("target_path")
    relationship.add_argument("relation")
    relationship.add_argument("evidence_json")
    relationship.add_argument("--source-source")
    relationship.add_argument("--target-source")
    relationship.add_argument("--actor", default="cli")
    ocr = commands.add_parser("ocr", help="manage the pinned local OCR runtime")
    ocr_commands = ocr.add_subparsers(dest="ocr_command", required=True)
    ocr_commands.add_parser("install")
    auth = commands.add_parser("auth-google", help="create a restricted Google OAuth token")
    auth.add_argument("--client-secret", type=Path, required=True)
    auth.add_argument("--token-file", type=Path, required=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    home = args.home.expanduser().resolve()
    if args.command == "init":
        root = (args.root or home).expanduser().resolve()
        if args.root and not root.is_dir():
            raise SystemExit(f"source root is not a directory: {root}")
        settings = Settings(
            root=root,
            home=home,
            exclusions=parse_exclusions(args.exclude),
            reconcile_seconds=args.reconcile_seconds,
        )
        settings.save()
        service = MultiSourceRAG(settings)
        _print({"initialized": True, **service.status()})
        return 0
    if args.command == "auth-google":
        from .drive import authorize_google

        _print({"token_file": str(authorize_google(args.token_file, args.client_secret))})
        return 0

    service = MultiSourceRAG(Settings.load(home))
    command = args.command
    output: object
    if command == "source":
        if args.source_command == "list":
            output = service.sources()
        elif args.source_command == "add-local":
            output = service.add_local_source(args.name, args.root, args.exclude)
        elif args.source_command == "add-drive":
            output = service.add_drive_source(
                args.name,
                args.root_id,
                args.account,
                args.token_file,
                args.shared_drive_id,
                args.exclude,
            )
        elif args.source_command in {"enable", "disable"}:
            output = service.enable_source(args.source, args.source_command == "enable")
        else:
            if not args.yes:
                raise SystemExit("source remove requires --yes; source files are never deleted")
            output = service.remove_source(args.source)
    elif command in {"scan", "sync", "reconcile"}:
        output = service.reconcile(args.source, args.target, full=args.full)
    elif command in {"reindex", "rebuild"}:
        if not args.all and not args.source and not args.target:
            raise SystemExit("reindex requires --all, --source, or --target")
        output = service.reconcile(
            args.source,
            args.target,
            force_index=True,
            reextract=args.reextract,
            full=bool(args.all),
        )
    elif command == "search":
        output = service.search(args.query, args.limit, args.source, args.folder, args.mode)
    elif command == "read":
        output = service.read(args.path, args.source, args.start, args.length)
    elif command == "status":
        output = service.status()
    elif command in {"serve", "watch"}:
        from .watcher import MultiSourceWatchService

        with suppress(KeyboardInterrupt):
            MultiSourceWatchService(service).run()
        return 0
    elif command == "mcp":
        from .mcp import run_sdk_server

        return run_sdk_server(service, args.profile)
    elif command == "review":
        if args.review_command == "list":
            output = service.reviews(args.status)
        elif args.review_command == "resolve":
            output = service.resolve_review(args.id, args.resolution)
        else:
            output = service.correct_review(
                args.id, args.text, _evidence(args.evidence_json), args.actor
            )
    elif command == "metadata":
        output = (
            service.metadata(args.path, args.source)
            if args.metadata_command == "get"
            else service.add_metadata(
                args.path,
                args.key,
                _json(args.value_json, "value_json"),
                _evidence(args.evidence_json),
                args.actor,
                args.source,
            )
        )
    elif command == "relationship":
        output = service.add_relationship(
            args.source_path,
            args.target_path,
            args.relation,
            _evidence(args.evidence_json),
            args.actor,
            args.source_source,
            args.target_source,
        )
    elif command == "ocr" and args.ocr_command == "install":
        output = service.ocr_runtime.install()
    else:
        raise AssertionError(f"unhandled command: {command}")
    _print(output)
    return 1 if isinstance(output, dict) and output.get("errors") else 0


def legacy_main(argv: Sequence[str] | None = None) -> int:
    return main(argv)


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
