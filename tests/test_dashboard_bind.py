"""The dashboard is published on loopback unless the owner opts in (DASHBOARD_BIND)."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _ports():
    doc = yaml.safe_load((ROOT / "config" / "docker-compose.yml").read_text())
    return [str(p) for p in doc["services"]["karakos"]["ports"]]


def test_dashboard_port_defaults_to_loopback():
    dash = [p for p in _ports() if "DASHBOARD_PORT" in p]
    assert dash == ["${DASHBOARD_BIND:-127.0.0.1}:${DASHBOARD_PORT:-3000}:${DASHBOARD_PORT:-3000}"]


def test_every_published_port_names_an_interface():
    for p in _ports():
        assert p.startswith("127.0.0.1:") or p.startswith("${DASHBOARD_BIND:-127.0.0.1}:"), p


def test_env_template_keeps_the_bind_commented_out():
    lines = (ROOT / "config" / ".env.template").read_text().splitlines()
    assert not [l for l in lines if l.startswith("DASHBOARD_BIND=")]
    assert [l for l in lines if l.startswith("# DASHBOARD_BIND=")]


def test_readme_diagram_does_not_claim_https():
    assert "https :3000" not in (ROOT / "README.md").read_text()


def test_dashboard_bind_is_documented():
    assert "DASHBOARD_BIND" in (ROOT / "docs" / "EXTENDING.md").read_text()
    assert "DASHBOARD_BIND" in (ROOT / "docs" / "ARCHITECTURE.md").read_text()
    assert "DASHBOARD_BIND" in (ROOT / "README.md").read_text()
