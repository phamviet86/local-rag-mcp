from __future__ import annotations

import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch

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
