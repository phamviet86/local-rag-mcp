from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anyio
import pytest

from local_rag.cli import main
from local_rag.config import Settings
from local_rag.jobs import daemon_alive, write_daemon_heartbeat
from local_rag.mcp import create_sdk_server
from local_rag.scheduler import SyncScheduler
from local_rag.service import MultiSourceRAG
from local_rag.watcher import MultiSourceWatchService
from tests.test_multisource import FakeDriveBackend


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def clock():
    return Clock(datetime.now(UTC).replace(microsecond=0))


@pytest.fixture
def service(tmp_path, clock):
    home = tmp_path / "home"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# Alpha\nalpha body", encoding="utf-8")
    settings = Settings(root=docs, home=home, chunk_chars=100, chunk_overlap=10)
    settings.save()
    rag = MultiSourceRAG(settings, clock=clock)
    return rag


def set_sync(service, name, moment):
    with service.db.transaction() as connection:
        connection.execute("UPDATE sources SET last_sync_at=? WHERE name=?", (stamp(moment), name))


def set_success(service, name, moment):
    record = service.registry.get(name)
    service.db.set_sync_state(record.id, "last_success_at", moment.strftime("%Y-%m-%dT%H:%M:%SZ"))


def set_job_created(service, job_id, moment):
    with service.db.transaction() as connection:
        connection.execute("UPDATE index_jobs SET created_at=? WHERE id=?", (stamp(moment), job_id))


def add_drive(service, tmp_path, **config):
    service.add_drive_source("drive", "root", "acct", tmp_path / "token.json")
    if config:
        service.set_source_schedule("drive", **config)


def test_source_schedule_config_and_defaults(service, tmp_path):
    add_drive(service, tmp_path)
    by_name = {item["name"]: item for item in service.sources()}
    assert by_name["drive"]["poll_seconds"] == 300
    assert by_name["default"]["poll_seconds"] == 600
    assert by_name["drive"]["full_reconcile_seconds"] == 43200
    updated = service.set_source_schedule("default", poll_seconds=120, full_reconcile_seconds=3600)
    assert updated["poll_seconds"] == 120 and updated["full_reconcile_seconds"] == 3600
    with pytest.raises(ValueError, match="at least 60"):
        service.set_source_schedule("default", poll_seconds=30)
    with pytest.raises(ValueError, match="provide"):
        service.set_source_schedule("default")


def test_freshness_fields_use_utc_z(service, clock):
    set_sync(service, "default", clock.now - timedelta(seconds=100))
    set_success(service, "default", clock.now - timedelta(seconds=500))
    public = service.sources()[0]
    last = clock.now - timedelta(seconds=100)
    success = clock.now - timedelta(seconds=500)
    assert public["last_synced_at"] == success.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert public["last_attempt_at"] == last.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert public["next_sync_at"] == (last + timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert service.status()["source_status"][0]["poll_seconds"] == 600


def test_scheduler_due_logic_with_fake_clock(service, clock):
    scheduler = SyncScheduler(service, clock)
    source = service.registry.get("default")
    assert scheduler.due(source) is None  # never synced: ages from scheduler start
    clock.advance(599)
    assert scheduler.due(source) is None
    clock.advance(2)
    assert scheduler.due(source) == "incremental"
    # A recent whole-source reconcile job (even without last_sync_at) postpones the next poll.
    job = service.enqueue_index_job("reconcile", "default")
    set_job_created(service, job["id"], clock.now - timedelta(seconds=10))
    assert scheduler.due(service.registry.get("default")) is None


def test_scheduler_enqueues_only_when_idle_and_most_overdue_first(service, clock, tmp_path):
    add_drive(service, tmp_path, poll_seconds=60)
    set_sync(service, "default", clock.now - timedelta(seconds=700))
    set_sync(service, "drive", clock.now - timedelta(seconds=900))
    scheduler = SyncScheduler(service, clock)
    first = scheduler.run_once()
    assert first is not None and first["active_source"] == "drive" and first["state"] == "queued"
    assert first["full"] is False
    assert scheduler.run_once() is None  # a job is active
    with service.db.transaction() as connection:
        connection.execute("UPDATE index_jobs SET state='complete' WHERE id=?", (first["id"],))
    # The drive job counts as recent, so the local source is next.
    second = scheduler.run_once()
    assert second is not None and second["active_source"] == "default"


def test_scheduler_full_reconcile_and_tracking(service, clock, tmp_path):
    add_drive(service, tmp_path, poll_seconds=60, full_reconcile_seconds=3600)
    scheduler = SyncScheduler(service, clock)
    drive = service.registry.get("drive")
    clock.advance(3601)
    set_sync(service, "default", clock.now)
    set_sync(service, "drive", clock.now)  # polled fine; only the full interval elapsed
    assert scheduler.due(service.registry.get("drive")) == "full"
    full_at = (clock.now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    service.db.set_sync_state(drive.id, "last_full_reconcile_at", full_at)
    assert scheduler.due(service.registry.get("drive")) is None
    clock.advance(3600)
    set_sync(service, "default", clock.now)
    set_sync(service, "drive", clock.now)
    assert scheduler.due(service.registry.get("drive")) == "full"
    job = scheduler.run_once()
    assert job is not None and job["full"] is True


def test_full_scan_records_last_full_reconcile(tmp_path, clock):
    home = tmp_path / "home"
    settings = Settings(root=home, home=home, chunk_chars=100, chunk_overlap=10)
    settings.save()
    backend = FakeDriveBackend([], {})
    rag = MultiSourceRAG(settings, drive_backend_factory=lambda source: backend, clock=clock)
    rag.add_drive_source("fixture", "root", "acct", tmp_path / "token.json")
    record = rag.registry.get("fixture")
    assert rag.db.sync_state(record.id, "last_full_reconcile_at") is None
    result = rag.reconcile("fixture", full=True)
    assert not result["errors"]
    assert rag.db.sync_state(record.id, "last_full_reconcile_at") == clock.now.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_daemon_tick_enqueues_then_claims_due_source(service, clock):
    daemon = MultiSourceWatchService(service, clock)
    daemon.tick(now=0.0)
    assert not service.index_status()["active"]  # nothing due yet
    clock.advance(601)
    daemon.tick(now=100.0)  # claims nothing, then schedules
    queued = service.index_status()["job"]
    assert queued is not None and queued["state"] == "queued"
    daemon.tick(now=200.0)  # claims and runs the queued job
    done = service.job_status(queued["id"])
    assert done["state"] == "complete" and done["active_source"] == "default"
    assert service.search("alpha")["results"]


def test_daemon_schedule_check_is_throttled(service, clock):
    daemon = MultiSourceWatchService(service, clock)
    calls = []
    daemon.scheduler.run_once = lambda: calls.append(1)  # type: ignore[method-assign]
    daemon.tick(now=0.0)
    daemon.tick(now=1.0)
    daemon.tick(now=6.0)
    assert len(calls) == 2


def test_refresh_enqueues_and_reports_executor(service, clock):
    first = service.refresh("default", background=False)
    assert first["state"] == "complete"
    assert first["job"]["executor"] == "this_process"
    assert "target" not in first["job"] and "error" not in first["job"]


def test_refresh_rate_limits_per_source(service, clock):
    first = service.refresh("default", background=False)
    set_job_created(service, first["job"]["id"], clock.now - timedelta(seconds=20))
    limited = service.refresh("default", background=False)
    assert limited["state"] == "rate_limited"
    assert limited["retry_after_seconds"] == 40
    set_job_created(service, first["job"]["id"], clock.now - timedelta(seconds=61))
    again = service.refresh("default", background=False)
    assert again["state"] == "complete"
    with pytest.raises(ValueError, match="source not found"):
        service.refresh("missing")


def test_refresh_rate_limit_is_per_source(service, clock, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "b.md").write_text("beta", encoding="utf-8")
    service.add_local_source("second", other)
    first = service.refresh("default", background=False)
    assert first["state"] == "complete"
    second = service.refresh("second", background=False)
    assert second["state"] == "complete"
    # No source: limited by the newest reconcile job of any source.
    assert service.refresh(None, background=False)["state"] == "rate_limited"


def test_refresh_coalesces_active_job(service, clock):
    beat = clock.now
    write_daemon_heartbeat(service.db, beat, now=datetime.now(UTC))
    assert daemon_alive(service.db)
    first = service.refresh("default")
    assert first["state"] == "queued" and first["job"]["executor"] == "daemon"
    second = service.refresh("default")
    assert second["state"] == "queued" and second["coalesced"] is True
    assert second["job"]["id"] == first["job"]["id"]
    assert len([job for job in service.list_jobs() if job["state"] == "queued"]) == 1


def test_refresh_without_enabled_sources(tmp_path):
    home = tmp_path / "home"
    settings = Settings(root=home, home=home)
    settings.save()
    result = MultiSourceRAG(settings).refresh()
    assert result["state"] == "unavailable"
    assert result["error"]["code"] == "no_enabled_sources"


def test_refresh_never_full_or_reindex(service):
    write_daemon_heartbeat(service.db, datetime.now(UTC), now=datetime.now(UTC))
    service.refresh("default")
    job = service.list_jobs(reader=False)[0]
    assert job["kind"] == "reconcile" and job["full"] is False and job["reextract"] is False


def test_refresh_mcp_tool_and_cli(service, tmp_path, capsys):
    server = create_sdk_server(service, "reader")

    async def call():
        return await server.call_tool("refresh", {"source": "default"})

    result = anyio.run(call)
    assert result.structured_content["job"]["id"]
    assert not result.is_error
    for thread in list(service.jobs._threads.values()):
        thread.join()
    code = main(["--home", str(service.settings.home), "refresh", "--source", "default"])
    assert code == 0
    assert '"rate_limited"' in capsys.readouterr().out


def test_cli_source_set(service, capsys):
    code = main(
        [
            "--home",
            str(service.settings.home),
            "source",
            "set",
            "default",
            "--poll-seconds",
            "90",
            "--full-reconcile-seconds",
            "7200",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert '"poll_seconds": 90' in out and '"full_reconcile_seconds": 7200' in out
    with pytest.raises(SystemExit):
        main(
            [
                "--home",
                str(service.settings.home),
                "source",
                "set",
                "default",
                "--poll-seconds",
                "5",
            ]
        )


def test_coverage_and_doctor_report_freshness_and_stale(service, clock):
    service.reconcile()
    coverage = service.search("alpha")["coverage"]
    entry = coverage["freshness"][0]
    assert entry["source"] == "default" and entry["poll_seconds"] == 600
    assert entry["last_synced_at"].endswith("Z") and entry["next_sync_at"].endswith("Z")
    sync = service.doctor()["checks"]["sync"]
    assert sync["stale_sources"] == [] and sync["sources"][0]["stale"] is False
    with service.db.transaction() as connection:
        connection.execute("UPDATE sources SET last_error=NULL")
    set_success(service, "default", clock.now - timedelta(seconds=1801))
    stale = service.doctor()
    assert stale["checks"]["sync"]["stale_sources"] == ["default"]
    assert stale["checks"]["sync"]["status"] == "warning"
    assert stale["ok"] is True
    set_success(service, "default", clock.now - timedelta(seconds=1700))
    assert service.doctor()["checks"]["sync"]["stale_sources"] == []


def test_failing_source_becomes_stale_despite_recent_attempts(service, clock):
    service.reconcile()
    assert service.sources()[0]["last_synced_at"]  # a successful sync records last_success_at
    set_success(service, "default", clock.now - timedelta(seconds=4000))
    # Failed attempts keep refreshing last_sync_at but must not refresh the success time.
    service.db.update_source_sync(service.registry.get("default").id, None, "boom")
    public = service.sources()[0]
    assert public["last_attempt_at"] and public["last_synced_at"].startswith(
        (clock.now - timedelta(seconds=4000)).strftime("%Y-%m-%dT%H:%M")
    )
    sync = service.doctor()["checks"]["sync"]
    assert sync["stale_sources"] == ["default"] and sync["status"] == "error"


def test_missing_last_full_falls_back_to_source_creation(service, clock, tmp_path):
    add_drive(service, tmp_path, poll_seconds=60, full_reconcile_seconds=3600)
    with service.db.transaction() as connection:
        connection.execute(
            "UPDATE sources SET created_at=? WHERE name='drive'",
            (stamp(clock.now - timedelta(seconds=7200)),),
        )
    set_sync(service, "drive", clock.now)
    # A freshly restarted scheduler must not postpone the full scan again.
    scheduler = SyncScheduler(service, clock)
    assert scheduler.due(service.registry.get("drive")) == "full"
