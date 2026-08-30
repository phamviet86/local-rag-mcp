from __future__ import annotations

import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch

import local_rag_mcp
from local_rag.cli import main
from local_rag.config import Settings
from local_rag.drive import DriveChanges, DriveItem, authorize_google
from local_rag.mcp import MCPServer, create_sdk_server
from local_rag.service import LocalRAG, MultiSourceRAG


class FakeDriveBackend:
    def __init__(self, items: list[DriveItem], content: dict[str, bytes]):
        self.items = items
        self.content = content
        self.next_changes = DriveChanges((), (), "cursor-2")
        self.downloads: list[str] = []

    def full_scan(self, root_id: str):
        return tuple(self.items), "cursor-1"

    def changes(self, root_id: str, cursor: str):
        return self.next_changes

    def download(self, item: DriveItem) -> bytes:
        self.downloads.append(item.id)
        return self.content[item.id]

    def close(self) -> None:
        pass


class FakeEmbeddings:
    identity = ("test", "shared-v1")

    @staticmethod
    def embed(texts):
        return [[float(len(text)), 1.0] for text in texts]


def drive_item(identifier: str, name: str, version: str, checksum: str) -> DriveItem:
    return DriveItem(
        identifier,
        name,
        "text/plain",
        f"team/{name}",
        f"2026-08-30T00:00:0{version}Z",
        version,
        checksum,
        f"https://drive.google.com/open?id={identifier}",
    )


class MultiSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.home = base / "home"
        self.root_a, self.root_b = base / "root-a", base / "root-b"
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.settings = Settings(root=self.home, home=self.home, chunk_chars=100, chunk_overlap=10)
        self.settings.save()

    def tearDown(self):
        self.temp.cleanup()

    def test_local_sources_global_scoped_disable_and_safe_remove(self):
        first, second = self.root_a / "shared.txt", self.root_b / "shared.txt"
        first.write_text("alpha orchard policy", encoding="utf-8")
        second.write_text("bravo ocean policy", encoding="utf-8")
        service = MultiSourceRAG(self.settings)
        service.add_local_source("alpha", self.root_a)
        service.add_local_source("bravo", self.root_b)
        report = service.reconcile()
        self.assertFalse(report["errors"])
        self.assertEqual(service.status()["documents"], 2)
        self.assertEqual(
            {hit["source"] for hit in service.search("policy", mode="full_text")["results"]},
            {"alpha", "bravo"},
        )
        scoped = service.search("policy", source="alpha", mode="full_text")
        self.assertEqual([hit["source"] for hit in scoped["results"]], ["alpha"])
        document_ref = scoped["results"][0]["document_ref"]
        read = service.read(document_ref)
        self.assertEqual(read["document_ref"], document_ref)
        self.assertEqual(read["source"], "alpha")
        self.assertEqual(read["source_kind"], "local")
        self.assertEqual(read["external_id"], "shared.txt")
        self.assertEqual(read["authority"], "local_filesystem")
        self.assertEqual(read["source_hash"], read["content_hash"])
        self.assertTrue(read["indexed_at"])
        self.assertTrue(read["provenance"])
        self.assertIn("automatic", service.metadata(document_ref))
        evidence = [{"path": "alpha:shared.txt", "locator": "line:1", "quote": "alpha orchard"}]
        service.add_relationship(
            "shared.txt",
            "shared.txt",
            "cross-source-link",
            evidence,
            "test-agent",
            source_source="alpha",
            target_source="bravo",
        )
        self.assertTrue(service.search("cross", mode="full_text")["results"])
        service.enable_source("alpha", False)
        self.assertEqual(
            [hit["source"] for hit in service.search("policy", mode="full_text")["results"]],
            ["bravo"],
        )
        removed = service.remove_source("alpha")
        self.assertEqual(removed["documents_purged"], 1)
        self.assertFalse(removed["source_files_removed"])
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(service.status()["documents"], 1)
        self.assertFalse(service.search("cross", mode="full_text")["results"])

    def test_source_purge_removes_only_unreferenced_vectors(self):
        (self.root_a / "shared.txt").write_text("shared vector content", encoding="utf-8")
        (self.root_b / "shared.txt").write_text("shared vector content", encoding="utf-8")
        (self.root_a / "unique.txt").write_text("unique alpha vector", encoding="utf-8")
        service = MultiSourceRAG(self.settings, embeddings=FakeEmbeddings())
        service.add_local_source("alpha", self.root_a)
        service.add_local_source("bravo", self.root_b)
        service.reconcile()
        self.assertEqual(service.status()["vectors"], 2)

        removed = service.remove_source("alpha")

        self.assertEqual(removed["vectors_purged"], 1)
        self.assertEqual(service.status()["vectors"], 1)
        self.assertTrue(service.search("shared", mode="semantic")["results"])

    def test_legacy_database_and_data_root_migrate_in_place(self):
        legacy_settings = Settings(root=self.root_a, home=self.home)
        legacy_settings.save()
        (self.root_a / "legacy.txt").write_text("legacy compatible content", encoding="utf-8")
        legacy = LocalRAG(legacy_settings)
        legacy.scan()
        migrated = MultiSourceRAG(Settings.load(self.home))
        self.assertEqual(migrated.sources()[0]["name"], "default")
        hit = migrated.search("compatible", mode="full_text")["results"][0]
        self.assertEqual(hit["source"], "default")
        self.assertEqual(hit["path"], "legacy.txt")

    def test_mocked_drive_full_incremental_cached_reindex_and_purge(self):
        first = drive_item("same-id", "drive.txt", "1", "checksum-1")
        backend = FakeDriveBackend([first], {"same-id": b"alpha drive marker"})
        service = MultiSourceRAG(self.settings, drive_backend_factory=lambda source: backend)
        service.add_drive_source(
            "drive-a", "root-a", "account-a", Path(self.temp.name) / "token-a.json"
        )
        initial = service.reconcile("drive-a", full=True)
        self.assertFalse(initial["errors"])
        self.assertEqual(backend.downloads, ["same-id"])
        hit = service.search("alpha", source="drive-a", mode="full_text")["results"][0]
        self.assertEqual(hit["citation"]["external_id"], "same-id")
        self.assertTrue(hit["citation"]["url"].startswith("https://drive.google.com/"))
        read = service.read(hit["document_ref"])
        self.assertEqual(read["source_kind"], "google_drive")
        self.assertEqual(read["external_id"], "same-id")
        self.assertEqual(read["url"], hit["citation"]["url"])
        self.assertEqual(read["source_revision"], first.fingerprint)
        self.assertEqual(read["authority"], "google_drive")
        self.assertTrue(read["indexed_at"])
        self.assertEqual(
            service.search("alpha", folder="team", mode="full_text")["results"][0]["source"],
            "drive-a",
        )

        with patch.object(service.extractor, "extract", side_effect=AssertionError("reextracted")):
            cached = service.reconcile("drive-a", force_index=True)
        self.assertFalse(cached["errors"])
        self.assertEqual(backend.downloads, ["same-id"])

        with patch.object(service.extractor, "extract", wraps=service.extractor.extract) as extract:
            reextracted = service.reconcile(
                "drive-a", target="team", force_index=True, reextract=True
            )
        self.assertFalse(reextracted["errors"])
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(backend.downloads, ["same-id"])

        changed = drive_item("same-id", "drive.txt", "2", "checksum-2")
        backend.content["same-id"] = b"bravo drive marker"
        backend.next_changes = DriveChanges((changed,), (), "cursor-2")
        updated = service.reconcile("drive-a")
        self.assertFalse(updated["errors"])
        self.assertEqual(backend.downloads, ["same-id", "same-id"])
        self.assertTrue(service.search("bravo", source="drive-a", mode="full_text")["results"])
        self.assertFalse(service.search("alpha", source="drive-a", mode="full_text")["results"])

        backend.next_changes = DriveChanges((), ("same-id",), "cursor-3")
        deleted = service.reconcile("drive-a")
        self.assertEqual(deleted["sources"][0]["removed"], 1)
        self.assertFalse(service.search("bravo", source="drive-a", mode="full_text")["results"])

        source_id = service.registry.get("drive-a").id
        raw_cache = service.settings.cache_dir / "sources" / source_id
        self.assertTrue(raw_cache.exists())
        removed = service.remove_source("drive-a")
        self.assertEqual(removed["documents_purged"], 0)
        self.assertFalse(raw_cache.exists())

    def test_drive_item_failure_retains_cursor_and_retries_change(self):
        initial_item = drive_item("retry-id", "retry.txt", "1", "checksum-1")
        backend = FakeDriveBackend([initial_item], {"retry-id": b"initial drive text"})
        service = MultiSourceRAG(self.settings, drive_backend_factory=lambda source: backend)
        service.add_drive_source(
            "drive-retry", "root", "account", Path(self.temp.name) / "token.json"
        )
        service.reconcile("drive-retry", full=True)
        self.assertEqual(service.registry.get("drive-retry").cursor, "cursor-1")

        changed = drive_item("retry-id", "retry.txt", "2", "checksum-2")
        backend.next_changes = DriveChanges((changed,), (), "cursor-2")
        with patch.object(backend, "download", side_effect=RuntimeError("offline failure")):
            failed = service.reconcile("drive-retry")

        self.assertTrue(failed["sources"][0]["errors"])
        self.assertEqual(service.registry.get("drive-retry").cursor, "cursor-1")
        backend.content["retry-id"] = b"retried drive success"
        retried = service.reconcile("drive-retry")
        self.assertFalse(retried["errors"])
        self.assertEqual(service.registry.get("drive-retry").cursor, "cursor-2")
        self.assertTrue(
            service.search("retried", source="drive-retry", mode="full_text")["results"]
        )

    def test_multiple_drive_accounts_can_share_file_ids(self):
        item_a = drive_item("same-id", "account-a.txt", "1", "checksum-a")
        item_b = drive_item("same-id", "account-b.txt", "1", "checksum-b")
        backends = {
            "drive-a": FakeDriveBackend([item_a], {"same-id": b"shared account alpha"}),
            "drive-b": FakeDriveBackend([item_b], {"same-id": b"shared account bravo"}),
        }
        service = MultiSourceRAG(
            self.settings, drive_backend_factory=lambda source: backends[source.name]
        )
        service.add_drive_source(
            "drive-a", "root-a", "account-a", Path(self.temp.name) / "token-a.json"
        )
        service.add_drive_source(
            "drive-b", "root-b", "account-b", Path(self.temp.name) / "token-b.json"
        )
        report = service.reconcile(full=True)
        self.assertFalse(report["errors"])
        self.assertEqual(service.status()["documents"], 2)
        hits = service.search("shared account", mode="full_text")["results"]
        self.assertEqual({hit["source"] for hit in hits}, {"drive-a", "drive-b"})
        self.assertEqual(service.read("same-id", source="drive-b")["path"], "team/account-b.txt")

    def test_drive_target_reextract_is_isolated_and_preserves_cursor(self):
        team = drive_item("team-id", "team.txt", "1", "checksum-team")
        other = replace(
            drive_item("other-id", "other.txt", "1", "checksum-other"),
            relative_path="other/other.txt",
        )
        backend = FakeDriveBackend(
            [team, other],
            {"team-id": b"team target marker", "other-id": b"other target marker"},
        )
        service = MultiSourceRAG(self.settings, drive_backend_factory=lambda source: backend)
        service.add_drive_source(
            "drive-target", "root", "account", Path(self.temp.name) / "token.json"
        )
        service.reconcile("drive-target", full=True)
        cursor = service.registry.get("drive-target").cursor

        with patch.object(service.extractor, "extract", wraps=service.extractor.extract) as extract:
            report = service.reconcile(
                "drive-target", target="team", force_index=True, reextract=True
            )

        self.assertFalse(report["errors"])
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(backend.downloads, ["team-id", "other-id"])
        self.assertEqual(service.registry.get("drive-target").cursor, cursor)
        self.assertTrue(service.search("other", source="drive-target", mode="full_text")["results"])

    def test_mocked_oauth_writes_private_token_and_redacts_credential_path(self):
        client_secret = Path(self.temp.name) / "client.json"
        client_secret.write_text('{"installed":{"client_id":"test-client"}}', encoding="utf-8")
        token = Path(self.temp.name) / "credentials" / "drive.json"

        class FakeCredentials:
            @staticmethod
            def to_json():
                return '{"refresh_token":"not-a-real-secret"}'

        class FakeFlow:
            @classmethod
            def from_client_secrets_file(cls, path, scopes):
                self = cls()
                self.path, self.scopes = path, scopes
                return self

            @staticmethod
            def run_local_server(port):
                self.assertEqual(port, 0)
                return FakeCredentials()

        package = types.ModuleType("google_auth_oauthlib")
        flow_module = types.ModuleType("google_auth_oauthlib.flow")
        flow_module.InstalledAppFlow = FakeFlow
        with patch.dict(
            sys.modules,
            {"google_auth_oauthlib": package, "google_auth_oauthlib.flow": flow_module},
        ):
            authorized = authorize_google(token, client_secret)

        self.assertEqual(authorized, token.resolve())
        self.assertTrue(token.is_file())
        if sys.platform != "win32":
            self.assertEqual(S_IMODE(token.stat().st_mode), 0o600)
        service = MultiSourceRAG(self.settings)
        public = service.add_drive_source("drive", "root", "account", token)
        self.assertTrue(public["token_configured"])
        self.assertNotIn("token_file", public["config"])

    def test_bm25_rank_order_page_citations_and_mcp_profiles(self):
        (self.root_a / "strong.txt").write_text("vat vat vat vat", encoding="utf-8")
        (self.root_a / "weak.txt").write_text("other vat text", encoding="utf-8")
        service = MultiSourceRAG(self.settings)
        service.add_local_source("local", self.root_a)
        service.reconcile()
        results = service.search("vat", mode="full_text")["results"]
        self.assertEqual(results[0]["path"], "strong.txt")
        self.assertIn("page", results[0]["citation"])
        reader = MCPServer(service, "reader")
        reviewer = MCPServer(service, "reviewer")
        admin = MCPServer(service, "admin")
        self.assertIn("sources", reader.allowed)
        self.assertNotIn("add_metadata", reader.allowed)
        self.assertIn("add_relationship", reviewer.allowed)
        self.assertNotIn("remove_source", reviewer.allowed)
        self.assertIn("remove_source", admin.allowed)
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            admin.call("remove_source", {"source": "local", "confirm": False})

    def test_cli_and_mcp_global_and_strict_scope_contracts(self):
        for root, marker in ((self.root_a, "alpha"), (self.root_b, "bravo")):
            hidden = root / ".hidden"
            hidden.mkdir()
            (hidden / "document.txt").write_text(f"contract scope {marker}", encoding="utf-8")
        dotted = self.root_a / "reports."
        dotted.mkdir()
        (dotted / "report.txt").write_text("contract dotted report", encoding="utf-8")
        service = MultiSourceRAG(self.settings)
        service.add_local_source("alpha", self.root_a)
        service.add_local_source("bravo", self.root_b)
        service.reconcile()

        mcp = MCPServer(service, "reader")
        global_mcp = mcp.call("search", {"query": "contract", "mode": "full_text"})
        self.assertEqual({hit["source"] for hit in global_mcp["results"]}, {"alpha", "bravo"})
        scoped_mcp = mcp.call(
            "search",
            {
                "query": "contract",
                "source": "alpha",
                "folder": ".hidden",
                "mode": "full_text",
            },
        )
        self.assertEqual(
            [(hit["source"], hit["path"]) for hit in scoped_mcp["results"]],
            [("alpha", ".hidden/document.txt")],
        )
        document_ref = scoped_mcp["results"][0]["document_ref"]
        self.assertEqual(mcp.call("read", {"path": document_ref})["document_ref"], document_ref)
        self.assertIn("automatic", mcp.call("metadata", {"path": document_ref}))
        self.assertEqual(
            service.search("contract", folder="reports.", mode="full_text")["results"][0]["path"],
            "reports./report.txt",
        )

        global_cli = self._cli_json("search", "contract", "--mode", "full_text")
        self.assertEqual({hit["source"] for hit in global_cli["results"]}, {"alpha", "bravo"})
        scoped_cli = self._cli_json(
            "search",
            "contract",
            "--mode",
            "full_text",
            "--source",
            "bravo",
            "--folder",
            ".hidden",
        )
        self.assertEqual(
            [(hit["source"], hit["path"]) for hit in scoped_cli["results"]],
            [("bravo", ".hidden/document.txt")],
        )
        cli_document_ref = scoped_cli["results"][0]["document_ref"]
        self.assertEqual(self._cli_json("read", cli_document_ref)["document_ref"], cli_document_ref)
        self.assertIn("automatic", self._cli_json("metadata", "get", cli_document_ref))

    def test_package_does_not_export_internal_service_api(self):
        self.assertEqual(local_rag_mcp.__all__, ["__version__"])
        self.assertFalse(hasattr(local_rag_mcp, "MultiSourceRAG"))

    def _cli_json(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--home", str(self.home), *arguments])
        self.assertEqual(result, 0)
        return json.loads(output.getvalue())

    def test_sdk_registration_uses_profile_specific_typed_tools(self):
        class FakeAnnotations:
            def __init__(self, **values):
                self.values = values

        class FakeSDK:
            def __init__(self, *args, **kwargs):
                self.registered = {}

            def tool(self, annotations=None):
                def decorate(function):
                    self.registered[function.__name__] = function
                    return function

                return decorate

        mcp_module = types.ModuleType("mcp")
        server_module = types.ModuleType("mcp.server")
        mcpserver_module = types.ModuleType("mcp.server.mcpserver")
        types_module = types.ModuleType("mcp.types")
        mcpserver_module.MCPServer = FakeSDK
        types_module.ToolAnnotations = FakeAnnotations
        modules = {
            "mcp": mcp_module,
            "mcp.server": server_module,
            "mcp.server.mcpserver": mcpserver_module,
            "mcp.types": types_module,
        }
        service = MultiSourceRAG(self.settings)
        with patch.dict(sys.modules, modules):
            reader = create_sdk_server(service, "reader")
            admin = create_sdk_server(service, "admin")
        self.assertEqual(
            set(reader.registered), {"search", "read", "status", "sources", "metadata", "reviews"}
        )
        self.assertIn("correct_page", admin.registered)
        self.assertIn("remove_source", admin.registered)


if __name__ == "__main__":
    unittest.main()
