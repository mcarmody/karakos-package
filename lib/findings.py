"""Findings: what the monitor currently believes is wrong."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

SEVERITIES = ("info", "warn", "critical")
_RANK = {"info": 0, "warn": 1, "critical": 2}


@dataclass
class Finding:
    key: str
    kind: str
    severity: str
    subject: str
    why: str
    since: str = ""
    detail: Optional[dict] = None


def make(kind, subject, severity, why, since="", detail=None) -> Finding:
    return Finding(f"{kind}:{subject}", kind, severity, subject, why, since, detail)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def summary_text(findings, generated_at: str = "") -> str:
    lines = [f"# Monitor summary {generated_at}".rstrip()]
    if not findings:
        lines.append("No findings.")
    else:
        for f in sorted(findings, key=lambda f: f.since, reverse=True):
            lines.append(f"- [{f.severity}] {f.key}: {f.why}")
    return "\n".join(lines[:40]) + "\n"


def write(workspace, findings, generated_at: str = "") -> None:
    d = Path(workspace) / "data" / "health"
    atomic_write(d / "findings.json", json.dumps(
        {"generated_at": generated_at, "findings": [asdict(f) for f in findings]}, indent=2))
    atomic_write(d / "summary.md", summary_text(findings, generated_at))


def read(workspace) -> list:
    try:
        data = json.loads((Path(workspace) / "data" / "health" / "findings.json").read_text())
        return [Finding(**{k: f.get(k) for k in ("key", "kind", "severity", "subject", "why")},
                        since=f.get("since") or "", detail=f.get("detail"))
                for f in data.get("findings", [])]
    except (OSError, ValueError, TypeError, AttributeError):
        return []
