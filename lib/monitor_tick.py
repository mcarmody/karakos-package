"""The monitor's once-a-minute pass: drift -> findings -> alerts.

`tick` is the only caller of `alerts.send` besides health-monitor's daily run.
Its heartbeat is touched by the scheduler wrapper (touching it here too would
make `previous_timestamp` equal to the current run and blind job-late).
"""
from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

import alerts
import drift
import findings as findings_mod
import heartbeats
import job_registry
import monitor_config
from findings import make

# 3.2b appends (name, fn(workspace, now, config) -> list[Finding]) here.
EXTRA_STAGES: list = []


def _health(workspace) -> Path:
    return Path(workspace) / "data" / "health"


def _load_json(path: Path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def tick(workspace, now=None, send=None):
    """Run one pass. `send(post) -> bool` defaults to alerts.send. Returns the
    findings written."""
    workspace = Path(workspace)
    now = now or datetime.now(timezone.utc)
    found, posts = [], []
    config = {k: v for k, v in monitor_config.DEFAULTS.items()}

    def stage(name, fn):
        try:
            found.extend(fn() or [])
        except Exception as e:
            found.append(make("monitor-stage-failed", name, "warn",
                              f"monitor stage {name} failed: {e}",
                              now.isoformat(), {"trace": traceback.format_exc()[-800:]}))

    def cfg_stage():
        nonlocal config
        config, fs = monitor_config.load(workspace)
        return fs

    stage("config", cfg_stage)
    jobs_holder = {}

    def drift_stage():
        jfs = []
        jobs = job_registry.load_jobs(workspace, jfs)
        jobs_holder["jobs"] = jobs
        live = _load_json(_health(workspace) / "scheduler-jobs.json")
        started = heartbeats.parse_ts((live or {}).get("started_at"))
        return jfs + drift.drift_report(jobs, live, heartbeats.read_all(workspace, jobs), started, now)

    stage("drift", drift_stage)
    for name, fn in EXTRA_STAGES:
        stage(name, lambda fn=fn: fn(workspace, now, config))

    # keep `since` stable across ticks
    prev = {f.key: f.since for f in findings_mod.read(workspace)}
    for f in found:
        f.since = prev.get(f.key) or f.since or now.isoformat()

    state_path = _health(workspace) / "alert-state.json"
    old_state = _load_json(state_path) or {"keys": {}, "recent": []}
    try:
        posts, new_state = alerts.plan(found, old_state, config, now)
        do_send = send or (lambda p: alerts.send(p, config, workspace))
        results = []
        for p in posts:
            try:
                results.append(bool(do_send(p)))
            except Exception:
                results.append(False)
        findings_mod.atomic_write(state_path, json.dumps(alerts.commit(old_state, new_state, posts, results)))
    except Exception as e:
        found.append(make("monitor-stage-failed", "alerts", "warn",
                          f"monitor stage alerts failed: {e}", now.isoformat()))
    findings_mod.write(workspace, found, now.isoformat())
    return found


def run():
    tick(os.environ.get("WORKSPACE_ROOT", "/workspace"))
