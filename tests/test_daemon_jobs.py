from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from local_rag.cli import main
from local_rag.config import Settings
from local_rag.jobs import daemon_alive, daemon_info, write_daemon_heartbeat
from local_rag.service import MultiSourceRAG
from local_rag.watcher import MultiSourceWatchService, PendingChange


@pytest.fixture
def service(tmp_path):
    home = tmp_path / "home"
    docs = tmp_path / "default"
    docs.mkdir()
    (docs / "a.md").write_text("# Alpha\nalpha body", encoding="utf-8")
    settings = Settings(root=docs, home=home, chunk_chars=100, chunk_overlap=10)
    settings.save()
    rag = MultiSourceRAG(settings)
    return rag


def age(service, job_id, minutes):
    with service.db.transaction() as connection:
        connection.execute(
            "UPDATE index_jobs SET created_at=datetime('now',?) WHERE id=?",
            (f"-{minutes} minutes", job_id),
        )


def set_state(service, job_id, state):
    with service.db.transaction() as connection:
        connection.execute("UPDATE index_jobs SET state=? WHERE id=?", (state, job_id))


def beat(service, seconds_ago=0.0):
    now = datetime.now(UTC)
    started = now - timedelta(minutes=5)
    write_daemon_heartbeat(service.db, started, now=now - timedelta(seconds=seconds_ago))
    return now


def test_stale_queued_job_expires_and_stops_blocking(service):
    old = service.enqueue_index_job("reconcile")
    age(service, old["id"], 25)
    fresh = service.enqueue_index_job("reindex", "default", full=True)
    assert fresh["state"] == "queued"
    stale = service.job_status(old["id"], reader=False)
    assert stale["state"] == "error"
    assert stale["error"] == "queued job was never started"


def test_recent_queued_job_still_blocks(service):
    service.enqueue_index_job("reconcile")
    other = service.enqueue_index_job("reindex", "default", full=True)
    assert other["state"] == "rejected"


def test_cli_does_not_rerun_job_held_by_another_process(service, tmp_path, capsys):
    job = service.enqueue_index_job("reconcile")
    with service.db.transaction() as connection:
        connection.execute(
            "UPDATE index_jobs SET state='running',phase='indexing',"
            "heartbeat_at=CURRENT_TIMESTAMP WHERE id=?",
            (job["id"],),
        )
        connection.execute(
            "UPDATE index_writer_lease SET job_id=?,heartbeat_at=CURRENT_TIMESTAMP",
            (job["id"],),
        )
    # Identical enqueue coalesces onto the running job; it must be returned, not executed again.
    again = service.enqueue_index_job("reconcile")
    assert again["state"] == "running"
    assert service.run_enqueued_job(again, defer_to_daemon=False)["state"] == "running"
    assert service.job_status(job["id"], reader=False)["state"] == "running"
    # The queue itself also refuses to run a job that is no longer queued.
    assert service.run_index_job(job["id"])["state"] == "running"

    main(["--home", str(service.settings.home), "reconcile"])
    assert service.job_status(job["id"], reader=False)["state"] == "running"
    assert '"state": "running"' in capsys.readouterr().out


def test_cli_returns_rejected_job_without_running(service, capsys):
    service.enqueue_index_job("reconcile")
    main(["--home", str(service.settings.home), "reindex", "--source", "default"])
    assert '"state": "rejected"' in capsys.readouterr().out


def test_heartbeat_and_daemon_alive_with_injected_clock(service):
    assert not daemon_alive(service.db)
    now = beat(service)
    assert daemon_alive(service.db, now=now + timedelta(seconds=29))
    assert not daemon_alive(service.db, now=now + timedelta(seconds=31))
    info = daemon_info(service.db, now=now)
    assert info is not None and info["heartbeat_at"].endswith("Z")
    assert info["started_at"].endswith("Z") and info["pid"] > 0
    beat(service, seconds_ago=120)
    assert not daemon_alive(service.db)


def test_watch_service_heartbeat_row(service):
    daemon = MultiSourceWatchService(service)
    daemon.heartbeat()
    assert daemon_alive(service.db)


def test_daemon_claims_queued_job(service):
    job = service.enqueue_index_job("reconcile")
    assert job["state"] == "queued"
    daemon = MultiSourceWatchService(service)
    daemon.tick()
    done = service.job_status(job["id"], reader=False)
    assert done["state"] == "complete"
    assert service.search("alpha")["results"]
    daemon.tick()  # nothing queued, nothing to do


def test_daemon_does_not_claim_while_a_job_runs(service):
    job = service.enqueue_index_job("reconcile")
    set_state(service, job["id"], "running")
    assert service.jobs.next_queued() is None
    assert service.run_next_queued_job() is None


def test_other_process_only_enqueues_when_daemon_alive(service):
    beat(service)
    job = service.start_index_job("reconcile", background=False)
    assert job["state"] == "queued" and job["executor"] == "daemon"
    assert service.job_status(job["id"], reader=False)["state"] == "queued"
    # The daemon itself (defer_to_daemon=False) runs the queued job directly.
    daemon = MultiSourceWatchService(service)
    daemon.tick()
    assert service.job_status(job["id"], reader=False)["state"] == "complete"
    own = service.start_index_job("reconcile", background=False, defer_to_daemon=False)
    assert own["state"] == "complete" and own["executor"] == "this_process"


def test_without_daemon_job_runs_in_calling_process(service):
    job = service.start_index_job("reconcile", background=False)
    assert job["state"] == "complete" and job["executor"] == "this_process"
    expired = beat(service, seconds_ago=300)
    assert expired
    job = service.start_index_job("reindex", "default", full=True, background=False)
    assert job["executor"] == "this_process" and job["state"] == "complete"


def test_rejected_file_event_is_retried(service, tmp_path):
    daemon = MultiSourceWatchService(service)
    source, handler = daemon.handlers[0]
    target = Path(source.locator) / "a.md"
    handler._pending[str(target.resolve())] = PendingChange("modified", target, None, 0.0)
    blocker = service.enqueue_index_job("reindex", "default", full=True)
    set_state(service, blocker["id"], "running")  # another process is indexing

    daemon._flush_source(source, handler, now=100.0)
    assert handler.pending_count == 1  # kept, not lost
    assert handler.drain_ready(now=100.5) == []  # honours the retry delay

    set_state(service, blocker["id"], "complete")
    daemon._flush_source(source, handler, now=102.0)
    assert handler.pending_count == 0
    retried = [job for job in service.list_jobs() if job["target"] == str(target)]
    assert [job["state"] for job in retried].count("rejected") == 1
    assert [job["state"] for job in retried].count("complete") == 1


def test_doctor_daemon_check(service):
    warning = service.doctor()
    assert warning["checks"]["daemon"]["status"] == "warning"
    assert "no serve daemon" in warning["checks"]["daemon"]["message"]
    assert warning["ok"] is True  # not blocking
    beat(service)
    alive = service.doctor()["checks"]["daemon"]
    assert alive["status"] == "ok"
    assert alive["pid"] > 0 and alive["hostname"] and alive["heartbeat_at"].endswith("Z")


def test_idle_tick_does_not_write(service, caplog):
    daemon = MultiSourceWatchService(service)
    daemon.tick()  # first tick may run the due sweep
    with service.db.connect() as connection:
        before = connection.execute("PRAGMA data_version").fetchone()[0]
    writes = []
    original = service.db.transaction
    service.db.transaction = lambda: (writes.append(1), original())[1]
    try:
        for _ in range(5):
            daemon.tick()
    finally:
        service.db.transaction = original
    assert writes == []
    with service.db.connect() as connection:
        assert connection.execute("PRAGMA data_version").fetchone()[0] == before
    assert service.jobs.next_queued(monotonic=lambda: 1e9) is None  # sweep when due


def test_tick_errors_are_logged_and_throttled(service, caplog):
    import logging

    daemon = MultiSourceWatchService(service)
    caplog.set_level(logging.DEBUG, logger="local_rag.watcher")
    daemon._log_tick_error(RuntimeError("index writer lease is held by another job"))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    daemon._log_tick_error(ValueError("boom"), now=100.0)
    daemon._log_tick_error(ValueError("boom"), now=110.0)
    daemon._log_tick_error(ValueError("boom"), now=170.0)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
