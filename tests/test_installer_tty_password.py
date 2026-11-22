"""Static checks for two installer defects.

- install.sh must read the overwrite answer from the terminal, not from stdin
  (under `curl ... | bash`, stdin is the script text).
- setup.sh must print the dashboard password only when stdout is a terminal.

The scripts are not run end to end here; these checks read their text.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_overwrite_prompt_reads_from_the_terminal():
    lines = (ROOT / "install.sh").read_text().splitlines()
    prompt = [line for line in lines if "Overwrite existing karakos install?" in line]
    assert len(prompt) == 1, prompt
    assert "< /dev/tty" in prompt[0], prompt[0]
    text = "\n".join(lines)
    assert "(: < /dev/tty)" in text


def test_password_is_printed_only_on_a_terminal():
    lines = (ROOT / "setup.sh").read_text().splitlines()
    idx = next(i for i, line in enumerate(lines)
               if 'echo "Dashboard password: $DASHBOARD_PASSWORD"' in line)
    before = lines[max(0, idx - 3):idx]
    assert any("[ -t 1 ]" in line for line in before), before
    assert "not printed because output is not a terminal" in "\n".join(lines)


def test_the_password_is_never_echoed_unconditionally():
    lines = (ROOT / "setup.sh").read_text().splitlines()
    count = sum(1 for line in lines if 'echo "Dashboard password: $DASHBOARD_PASSWORD"' in line)
    assert count == 1, count
