"""
Tests for bin/health-monitor.py — Component health checking.
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import import_script, PACKAGE_ROOT


class TestHealthFileChecks:
    """Test health file freshness detection."""

    def _make_monitor(self, tmp_workspace, monkeypatch):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        return import_script("health-monitor")

    def test_healthy_file_passes(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        now = datetime.now().isoformat()
        (health_dir / "relay.json").write_text(json.dumps({"timestamp": now}))

        healthy, reason = monitor.check_health_file("relay.json", 300)
        assert healthy is True
        assert reason == ""

    def test_stale_file_fails(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        old = (datetime.now() - timedelta(minutes=10)).isoformat()
        (health_dir / "relay.json").write_text(json.dumps({"timestamp": old}))

        healthy, reason = monitor.check_health_file("relay.json", 300)
        assert healthy is False
        assert "stale" in reason

    def test_missing_file_fails(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        healthy, reason = monitor.check_health_file("nonexistent.json", 300)
        assert healthy is False
        assert "missing" in reason

    def test_empty_timestamp_fails(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        (health_dir / "relay.json").write_text(json.dumps({"timestamp": ""}))

        healthy, reason = monitor.check_health_file("relay.json", 300)
        assert healthy is False
        assert "no timestamp" in reason

    def test_malformed_json_fails(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        (health_dir / "relay.json").write_text("not json")

        healthy, reason = monitor.check_health_file("relay.json", 300)
        assert healthy is False
        assert "error" in reason

    def test_memory_has_longer_threshold(self, tmp_workspace, monkeypatch):
        """Memory consolidation only runs daily — 48h threshold."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        old = (datetime.now() - timedelta(hours=24)).isoformat()
        (health_dir / "memory-consolidate.json").write_text(json.dumps({"timestamp": old}))

        healthy, _ = monitor.check_health_file("memory-consolidate.json", 172800)
        assert healthy is True

    def test_utc_aware_timestamp_is_fresh(self, tmp_workspace, monkeypatch):
        """mcp/tools-server.py writes `datetime.now(timezone.utc)`.

        Every other test in this file writes a naive stamp, which is why this
        went unnoticed: comparing an aware stamp against a naive `datetime.now()`
        raises TypeError, the broad except turns it into an "error" reason, and
        a healthy tools server reported unhealthy on every run.
        """
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        now = datetime.now(timezone.utc).isoformat()
        (health_dir / "mcp-tools.json").write_text(json.dumps({"timestamp": now}))

        healthy, reason = monitor.check_health_file("mcp-tools.json", 600)
        assert healthy is True, reason
        assert reason == ""

    def test_utc_aware_timestamp_still_detects_staleness(self, tmp_workspace, monkeypatch):
        """Normalising tzinfo must not paper over a genuinely stale file."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        (health_dir / "mcp-tools.json").write_text(json.dumps({"timestamp": old}))

        healthy, reason = monitor.check_health_file("mcp-tools.json", 600)
        assert healthy is False
        assert "stale" in reason

    def test_z_suffix_timestamp_is_fresh(self, tmp_workspace, monkeypatch):
        """The `Z` -> `+00:00` rewrite only matters if the compare then works."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        health_dir = tmp_workspace / "data" / "health"

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
        (health_dir / "relay.json").write_text(json.dumps({"timestamp": now}))

        healthy, reason = monitor.check_health_file("relay.json", 300)
        assert healthy is True, reason

    def test_every_job_and_component_names_a_file_something_writes(self, tmp_workspace, monkeypatch):
        """A table entry whose file has no writer is a permanent false alarm.

        `memory.json` was in the old table for months and nothing ever wrote
        it. The writer list is derived from the job table: components are
        written by their own process, scheduled jobs by the scheduler wrapper
        (lib/heartbeats.py), and a job's extra health file by its own script.
        """
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        import job_registry

        component_writers = {
            "mcp-tools": PACKAGE_ROOT / "mcp" / "tools-server.py",
            "relay": PACKAGE_ROOT / "bin" / "relay.py",
            "scheduler": PACKAGE_ROOT / "bin" / "scheduler.py",
        }
        health_file_writers = {
            "memory-consolidate.json": PACKAGE_ROOT / "lib" / "monitor_jobs" / "memory_consolidate.py",
        }
        files = monitor._health_files()
        for job in job_registry.BUILTIN:
            if job.kind == "component":
                assert f"{job.name}.json" in files
                writer = component_writers[job.name]
                assert f"{job.name}.json" in writer.read_text(), (
                    f"{writer.name} does not write {job.name}.json")
            elif job.health_file:
                assert job.health_file in files
                assert job.health_file in health_file_writers[job.health_file].read_text()
        assert set(files) == {"mcp-tools.json", "relay.json", "scheduler.json",
                              "memory-consolidate.json"}
        sched = (PACKAGE_ROOT / "bin" / "scheduler.py").read_text()
        assert "heartbeats.touch" in sched and "job_registry.load_jobs" in sched

    def test_verdict_shape(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        v = monitor.verdict()
        assert set(v) == {"healthy", "issues", "components"}
        assert set(v["components"]) == {"mcp-tools.json", "relay.json", "scheduler.json",
                                        "memory-consolidate.json"}
        assert v["healthy"] is False and len(v["issues"]) == 4


class TestPokeAlertPath:
    """The alert path has to tell the truth about whether it alerted.

    It posts through bin/discord-notify.sh directly (never an agent queue)."""

    def _make_monitor(self, tmp_workspace, monkeypatch, with_script=True):
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_workspace))
        if with_script:
            script = tmp_workspace / "bin" / "discord-notify.sh"
            script.write_text("#!/bin/sh\n")
            script.chmod(0o755)
        return import_script("health-monitor")

    def test_returns_false_when_notify_script_is_missing(self, tmp_workspace, monkeypatch):
        """A missing script raises OSError, not CalledProcessError."""
        monitor = self._make_monitor(tmp_workspace, monkeypatch, with_script=False)
        assert not (tmp_workspace / "bin" / "discord-notify.sh").exists()

        assert monitor.poke_signals("boom") is False

    def test_returns_false_on_nonzero_exit(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        def fake_run(*a, **kw):
            raise subprocess.CalledProcessError(1, "discord-notify.sh", stderr=b"discord 500")

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)
        assert monitor.poke_signals("boom") is False
        assert "discord 500" in (tmp_workspace / "logs" / "health-alerts.log").read_text()

    def test_returns_false_on_timeout(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)

        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired("discord-notify.sh", 30)

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)
        assert monitor.poke_signals("boom") is False

    def test_passes_a_timeout_and_uses_discord_notify(self, tmp_workspace, monkeypatch):
        monitor = self._make_monitor(tmp_workspace, monkeypatch)
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen.update(kw)
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(monitor.subprocess, "run", fake_run)
        assert monitor.poke_signals("boom") is True
        assert seen.get("timeout")
        assert seen["cmd"][0].endswith("discord-notify.sh")
        assert seen["cmd"][1:] == ["signals", "boom"]
