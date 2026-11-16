"""install.sh stops cleanly after installing Docker when the new group is not active yet."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="exercises the Linux branch")


def _tool(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def test_stops_with_log_out_message_and_exit_zero(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    for real in ("sh", "bash", "uname", "chmod"):
        (fake / real).symlink_to(shutil.which(real))
    _tool(fake / "git", 'echo "git version 2.0.0"\n')
    _tool(fake / "sudo", "exit 0\n")
    # `curl ... | sh` receives this script: it creates a docker that refuses every command,
    # which is what a new group looks like before the next login.
    _tool(fake / "curl",
          r"""echo 'printf "#!/bin/sh\nexit 1\n" > "$FAKEBIN/docker"; chmod +x "$FAKEBIN/docker"'""" + "\n")
    env = {"PATH": str(fake), "HOME": str(tmp_path), "USER": "tester", "FAKEBIN": str(fake)}
    r = subprocess.run([shutil.which("bash"), str(ROOT / "install.sh")], env=env, cwd=tmp_path,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "Log out and back in" in out, out
    assert "not running" not in out, out
    assert "systemctl start docker" not in out, out
    assert not (tmp_path / "karakos").exists()      # stopped before cloning
