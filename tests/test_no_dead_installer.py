"""The unused Go installer is gone and the real install paths remain (6.4)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_installer_dir_is_gone():
    assert not (ROOT / "installer").exists()


def test_install_scripts_remain():
    assert (ROOT / "install.sh").is_file()
    assert (ROOT / "install.ps1").is_file()


def test_jekyll_exclude_does_not_mention_it():
    assert "installer" not in (ROOT / "_config.yml").read_text()


def test_sleep_poll_hook_stays_gone():
    assert not (ROOT / "system" / "hooks" / "rewrite-sleep-poll.py").exists()
