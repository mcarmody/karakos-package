"""Upgrade smoke on tagged images (7.3a): runs tests/smoke/upgrade.sh <tag>.
Docker only; skips with a reason without `docker`, fails under
KARAKOS_REQUIRE_DOCKER=1. `v1.4.1` is the optional bucket check: it runs only
with KARAKOS_SMOKE_OPTIONAL=1.
"""

import os
import subprocess
from pathlib import Path

import pytest

from realcli_support import require_docker

pytestmark = [pytest.mark.slow, pytest.mark.docker]

SCRIPT = Path(__file__).parent / "smoke" / "upgrade.sh"
TAGS = ["v1.0.0", "v1.1.1", "v1.3", "v1.5.0",
        pytest.param("v1.4.1", marks=pytest.mark.skipif(
            os.environ.get("KARAKOS_SMOKE_OPTIONAL") != "1",
            reason="optional bucket check; set KARAKOS_SMOKE_OPTIONAL=1"))]


@pytest.mark.parametrize("tag", TAGS)
def test_upgrade_from_tag(tag):
    require_docker()
    proc = subprocess.run(["bash", str(SCRIPT), tag], capture_output=True, text=True, timeout=1500)
    assert proc.returncode == 0, f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    assert f"upgrade smoke [{tag}]: PASS" in proc.stdout
