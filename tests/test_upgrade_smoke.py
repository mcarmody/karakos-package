"""Upgrade smoke on tagged images (7.3a): runs tests/smoke/upgrade.sh <tag>.
Docker only; skips with a reason without `docker`, fails under
KARAKOS_REQUIRE_DOCKER=1. `v1.0.0`, `v1.1.1` and `v1.4.1` are optional bucket checks: they run
only with KARAKOS_SMOKE_OPTIONAL=1.
"""

import os
import subprocess
from pathlib import Path

import pytest

from realcli_support import require_docker

pytestmark = [pytest.mark.slow, pytest.mark.docker]

SCRIPT = Path(__file__).parent / "smoke" / "upgrade.sh"
_OPTIONAL = pytest.mark.skipif(
    os.environ.get("KARAKOS_SMOKE_OPTIONAL") != "1",
    reason="optional bucket check; set KARAKOS_SMOKE_OPTIONAL=1")
# v1.0.0 and v1.1.1: no published image, and the tag's own Dockerfile cannot build any
# more (no dashboard/package-lock.json; with `npm install` instead of `npm ci`, v1.0.0
# fails on a missing @/lib/hooks module and v1.1.1 on a Next.js route type error).
TAGS = [pytest.param("v1.0.0", marks=_OPTIONAL), pytest.param("v1.1.1", marks=_OPTIONAL),
        "v1.3", "v1.5.0", pytest.param("v1.4.1", marks=_OPTIONAL)]


@pytest.mark.parametrize("tag", TAGS)
def test_upgrade_from_tag(tag):
    require_docker()
    proc = subprocess.run(["bash", str(SCRIPT), tag], capture_output=True, text=True, timeout=1500)
    assert proc.returncode == 0, f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    assert f"upgrade smoke [{tag}]: PASS" in proc.stdout
