from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
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
    initialize.add_argument("--reconcile-seconds", type=float, default=600)
    setup = commands.add_parser("setup", help="explicitly configure full OCR or no-OCR mode")
    setup.add_argument("root", type=Path, nargs="?")
    setup.add_argument("--exclude", action="append", default=[])
    setup.add_argument("--reconcile-seconds", type=float, default=600)
    setup_mode = setup.add_mutually_exclusive_group(required=True)
    setup_mode.add_argument("--full", action="store_true", help="provision and verify local OCR")
    setup_mode.add_argument(
        "--no-ocr", action="store_true", help="index supported files without local OCR"
    )

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
        execution = command.add_mutually_exclusive_group()
        execution.add_argument(
            "--background", action="store_true", help="queue and return a job ID"
        )
        execution.add_argument("--wait", action="store_true", help="wait for completion (default)")
    reindex = commands.add_parser(
        "reindex", aliases=["rebuild"], help="rebuild file/folder/source/all index state"
    )
    reindex.add_argument("--source")
    reindex.add_argument("--target", help="relative file or folder within the source")
    reindex.add_argument("--all", action="store_true")
    reindex.add_argument(
        "--reextract", action="store_true", help="deliberately rerun extraction/OCR"
    )
    reindex_execution = reindex.add_mutually_exclusive_group()
    reindex_execution.add_argument(
        "--background", action="store_true", help="queue and return a job ID"
    )
    reindex_execution.add_argument("--wait", action="store_true", help="wait for completion")

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
    doctor = commands.add_parser("doctor", help="run actionable readiness checks")
    doctor.add_argument("--json", action="store_true", help="emit the stable JSON contract")
    commands.add_parser(
        "serve", aliases=["watch"], help="watch local roots and periodically sync all sources"
    )
    service_command = commands.add_parser("service", help="manage optional continuous indexing")
    service_commands = service_command.add_subparsers(dest="service_command", required=True)
    for name in ("install", "status", "start", "stop", "uninstall"):
        service_commands.add_parser(name)
    jobs = commands.add_parser("jobs", help="inspect or run durable indexing jobs")
    job_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    job_list = job_commands.add_parser("list")
    job_list.add_argument("--limit", type=int, default=50)
    job_status = job_commands.add_parser("status")
    job_status.add_argument("id")
    job_run = job_commands.add_parser("run", help=argparse.SUPPRESS)
    job_run.add_argument("id")
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
    if args.command in {"init", "setup"}:
        root = (args.root or home).expanduser().resolve()
        if args.root and not root.is_dir():
            raise SystemExit(f"source root is not a directory: {root}")
        settings = Settings(
            root=root,
            home=home,
            exclusions=parse_exclusions(args.exclude),
            reconcile_seconds=args.reconcile_seconds,
            ocr_mode=("full" if args.command == "setup" and args.full else "no-ocr"),
        )
        settings.save()
        service = MultiSourceRAG(settings)
        setup_status = service.status()
        next_step = setup_status.pop("error", None)
        if args.command == "setup" and args.full:
            try:
                manifest = service.ocr_runtime.provision_and_verify()
            except Exception as exc:
                _print(
                    {
                        "ok": False,
                        "error": {
                            "code": "ocr_setup_failed",
                            "message": str(exc),
                            "actions": [
                                {"command": "local-rag-mcp setup --no-ocr"},
                                {"command": "local-rag-mcp setup --full"},
                            ],
                        },
                    }
                )
                return 2
            setup_status = service.status()
            next_step = setup_status.pop("error", None)
            _print(
                {
                    "ok": True,
                    "initialized": True,
                    "ocr": manifest,
                    "next_step": next_step,
                    "service_recommendation": (
                        "optional: run 'local-rag-mcp service install' then "
                        "'local-rag-mcp service start'"
                    ),
                    **setup_status,
                }
            )
        else:
            _print(
                {
                    "ok": True,
                    "initialized": True,
                    "warning": "OCR-routed PDF pages will enter review until full OCR is set up.",
                    "next_step": next_step,
                    "service_recommendation": (
                        "optional: run 'local-rag-mcp service install' then "
                        "'local-rag-mcp service start'"
                    ),
                    **setup_status,
                }
            )
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
            output = service.source_summary()
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
        job = service.enqueue_index_job("reconcile", args.source, args.target, full=args.full)
        if args.background:
            _spawn_job(home, str(job["id"]))
            output = job
        else:
            output = service.run_index_job(str(job["id"]))
    elif command in {"reindex", "rebuild"}:
        if not args.all and not args.source and not args.target:
            raise SystemExit("reindex requires --all, --source, or --target")
        job = service.enqueue_index_job(
            "reindex",
            args.source,
            args.target,
            reextract=args.reextract,
            full=bool(args.all),
        )
        if args.background:
            _spawn_job(home, str(job["id"]))
            output = job
        else:
            output = service.run_index_job(str(job["id"]))
    elif command == "search":
        output = service.search(args.query, args.limit, args.source, args.folder, args.mode)
    elif command == "read":
        output = service.read(args.path, args.source, args.start, args.length)
    elif command == "status":
        output = service.status()
    elif command == "doctor":
        output = service.doctor()
    elif command == "jobs":
        if args.jobs_command == "list":
            output = service.list_jobs(limit=args.limit)
        elif args.jobs_command == "status":
            output = service.job_status(args.id, reader=False)
        else:
            output = service.run_index_job(args.id)
    elif command == "service":
        from .service_manager import AutoIndexService

        manager = AutoIndexService(home)
        output = getattr(manager, args.service_command)()
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
        output = service.ocr_runtime.provision_and_verify()
        replace(service.settings, ocr_mode="full").save()
    else:
        raise AssertionError(f"unhandled command: {command}")
    _print(output)
    if isinstance(output, dict):
        if output.get("error") or output.get("ok") is False:
            return 2
        if output.get("errors"):
            return 1
    return 0


def legacy_main(argv: Sequence[str] | None = None) -> int:
    return main(argv)


def entrypoint(argv: Sequence[str] | None = None) -> int:
    try:
        return main(argv)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        _print(
            {
                "ok": False,
                "error": {
                    "code": type(exc).__name__.removesuffix("Error").lower(),
                    "message": str(exc),
                    "actions": [],
                },
            }
        )
        return 2


def legacy_entrypoint(argv: Sequence[str] | None = None) -> int:
    return entrypoint(argv)


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


def _spawn_job(home: Path, job_id: str) -> None:
    subprocess.Popen(
        [sys.executable, "-m", "local_rag.cli", "--home", str(home), "jobs", "run", job_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


if __name__ == "__main__":
    raise SystemExit(entrypoint())
