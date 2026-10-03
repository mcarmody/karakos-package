"""Fresh-install smoke (7.3a): runs tests/smoke/fresh_install.sh. Docker only.

Skips with a reason when `docker` is absent; fails instead when
KARAKOS_REQUIRE_DOCKER=1. Needs the image: KARAKOS_SMOKE_IMAGE_TAR (a
`docker save` file) or the dashboard source for a local `docker build`.
"""

import subprocess
from pathlib import Path

import pytest

from realcli_support import require_docker

pytestmark = [pytest.mark.slow, pytest.mark.docker]

SCRIPT = Path(__file__).parent / "smoke" / "fresh_install.sh"


def test_fresh_install_comes_up_and_answers():
    require_docker()
    proc = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, f"{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}"
    assert "fresh-install smoke: PASS" in proc.stdout
