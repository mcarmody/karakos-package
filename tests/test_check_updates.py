"""
Tests for the weekly release check (#152).

The job was broken in two independent ways at once, and both are the kind that
leave no trace:

1. **It could not run.** It read `$WORKSPACE_ROOT/package.json` to learn the
   installed version. There is no package.json at the workspace root — the only
   one is dashboard/package.json — so under `set -euo pipefail` the script died
   on that line, every Monday, since it was written.

2. **Even succeeding, it told nobody.** It only `log()`ged to stdout, and
   bin/scheduler.py ran it with `capture_output=True`, which throws stdout away.

So the tests below pin the two things that make it a working check rather than
a working script: the version comes from a source that exists and is current,
and a human hears about an available release.

Everything drives the real script as a subprocess. `poke.sh` and
`discord-notify.sh` are recording stand-ins on disk, and the releases API is a
local JSON file served over `curl`'s file:// — so deleting the notification or
switching it to the other channel fails these tests on a wrong recording, not
on a mock mismatch.

Four properties are load-bearing and each is pinned:

1. **The installed version is the image tag.** config/docker-compose.yml
   resolves `${KARAKOS_VERSION:-latest}`, and every version-bearing file in the
   tree is frozen at the installer's "1.0.0" — .karakos/config.json and
   dashboard/package.json both, forever, on every release.
2. **`latest` is not a version number.** On a default install there is nothing
   to compare, and the check has to say something true anyway.
3. **The notice goes THROUGH poke.sh**, unlike bin/cli-upgrade-watchdog.sh,
   which deliberately bypasses it. An available update is not an outage.
4. **A failure is loud enough for the scheduler to log.** A weekly job that
   exits 0 on failure is invisible for a month.
"""

import ast
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).parent.parent
CHECK_UPDATES = PACKAGE_ROOT / "bin" / "check-updates.sh"
SCHEDULER = PACKAGE_ROOT / "bin" / "scheduler.py"

CHECKED = 0
CANNOT_CHECK = 1


def write_exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture
def world(tmp_path):
    """A releases API on disk, two recording notifiers, and no package.json.

    The workspace root here is deliberately shaped like a real install: it has
    a dashboard/package.json and a .karakos/config.json, both stamped "1.0.0",
    and no package.json at the root. That is the tree the old script died on.
    """
    workspace = tmp_path / "workspace"
    (workspace / "dashboard").mkdir(parents=True)
    (workspace / "dashboard" / "package.json").write_text(
        json.dumps({"name": "karakos-dashboard", "version": "1.0.0"})
    )
    (workspace / ".karakos").mkdir()
    (workspace / ".karakos" / "config.json").write_text(
        json.dumps({"version": "1.0.0", "system_name": "Karakos"})
    )

    notify_dir = tmp_path / "notify"
    notify_dir.mkdir()
    poke_log = notify_dir / "poke.log"
    notify_log = notify_dir / "notify.log"
    poke_rc = tmp_path / "poke-rc"
    poke_rc.write_text("0")

    write_exec(notify_dir / "poke.sh", f"""#!/usr/bin/env bash
printf "%s\\n" "$*" >> "{poke_log}"
exit "$(cat "{poke_rc}")"
""")
    write_exec(notify_dir / "discord-notify.sh",
               f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{notify_log}"\n')

    release = tmp_path / "release.json"

    class World:
        root = tmp_path
        state_file = tmp_path / "karakos-update.json"

        def publish(self, tag, html_url=None):
            body = {"tag_name": tag}
            if html_url is not None:
                body["html_url"] = html_url
            release.write_text(json.dumps(body))

        def publish_raw(self, text):
            release.write_text(text)

        def break_poke(self):
            poke_rc.write_text("1")

        def run(self, *args, url=None, **envextra):
            env = {
                **os.environ,
                "WORKSPACE_ROOT": str(workspace),
                "KARAKOS_NOTIFY_BIN_DIR": str(notify_dir),
                "KARAKOS_UPDATE_STATE_FILE": str(self.state_file),
                "KARAKOS_RELEASES_URL": url or f"file://{release}",
            }
            env.pop("KARAKOS_VERSION", None)
            env.update(envextra)
            return subprocess.run(
                ["bash", str(CHECK_UPDATES), *args],
                capture_output=True, text=True, timeout=60, env=env,
            )

        @property
        def pokes(self):
            if not poke_log.exists():
                return []
            return [l for l in poke_log.read_text().splitlines() if l.strip()]

        @property
        def notices(self):
            if not notify_log.exists():
                return []
            return [l for l in notify_log.read_text().splitlines() if l.strip()]

        @property
        def announced(self):
            if not self.state_file.exists():
                return None
            return json.loads(self.state_file.read_text()).get("last_announced")

    w = World()
    w.publish("v1.4", "https://github.com/mcarmody/karakos-package/releases/tag/v1.4")
    return w


# =============================================================================
# 1. The version comes from a source that exists
# =============================================================================

def test_the_check_completes_on_a_tree_with_no_root_package_json(world):
    """The original bug, reduced to one assertion.

    A real install has no package.json at the workspace root, and the script
    read one under `set -e`. Every weekly run since it was written died on
    line 10 with `cat: /workspace/package.json: No such file or directory`.
    """
    result = world.run(KARAKOS_VERSION="v1.4")

    assert result.returncode == CHECKED, result.stdout + result.stderr
    assert "No such file" not in result.stderr
    assert not (world.root / "workspace" / "package.json").exists(), \
        "the fixture stopped reproducing the tree the bug needed"


def test_the_installed_version_is_the_image_tag_docker_compose_resolves(world):
    """`${KARAKOS_VERSION:-latest}` is the only answer that cannot disagree
    with the code actually running — it is the literal expression
    config/docker-compose.yml uses to pick the image.
    """
    result = world.run(KARAKOS_VERSION="v1.2")

    assert "v1.2" in result.stdout
    assert world.pokes and "v1.2" in world.pokes[0]


def test_the_frozen_1_0_0_files_are_not_used_as_the_version(world):
    """setup.sh writes `"version": "1.0.0"` into .karakos/config.json as a
    string literal, on every install of every release, and never rewrites it on
    upgrade; dashboard/package.json is frozen the same way and versions the
    Next.js app rather than the release.

    Either one would make every install on earth permanently report "you are on
    1.0.0" — a false positive every single week, which is worse than the silence
    it replaced. The fixture stamps both files 1.0.0 while the tag says v1.4, so
    reading either shows up as a spurious update notice.
    """
    result = world.run(KARAKOS_VERSION="v1.4")

    assert result.returncode == CHECKED
    assert world.pokes == [], "an install already on the newest release was told to upgrade"
    assert "1.0.0" not in result.stdout


def test_the_script_does_not_read_a_version_out_of_any_package_json():
    """Belt and braces on the behavioural test: no code path names it at all,
    including a helpfully-relocated `dashboard/package.json`.
    """
    body = "\n".join(l for l in CHECK_UPDATES.read_text().splitlines()
                     if not l.strip().startswith("#"))
    assert "package.json" not in body


# =============================================================================
# 2. The `latest` case
# =============================================================================

def test_an_unpinned_install_is_not_compared_against_a_version_number(world):
    """A default install runs `latest`, so there is no version to compare and
    "latest" != "v1.4" is a string difference, not a finding.

    What is still true and worth saying: the release exists, and a `latest` tag
    only moves when the image is pulled. So the notice reports the release and
    the pull command, and never claims the install is on version "latest".
    """
    result = world.run()          # KARAKOS_VERSION unset, as on a default install

    assert result.returncode == CHECKED
    assert world.pokes, "nothing was said about a release on an unpinned install"
    message = world.pokes[0]
    assert "v1.4" in message
    assert "docker compose pull" in message
    assert "latest" in message
    assert "pinned to `latest`" not in message
    assert "vlatest" not in message


def test_an_image_tag_that_is_not_a_version_says_so_rather_than_guessing(world):
    """Someone can pin KARAKOS_VERSION to anything the registry accepts. The
    honest answer is that it cannot be compared, not a made-up comparison.
    """
    result = world.run(KARAKOS_VERSION="main")

    assert result.returncode == CHECKED
    assert world.pokes
    assert "cannot tell whether it is behind" in world.pokes[0]


# =============================================================================
# 3. Up to date vs behind
# =============================================================================

def test_an_up_to_date_install_exits_zero_and_pages_nobody(world):
    result = world.run(KARAKOS_VERSION="v1.4")

    assert result.returncode == CHECKED
    assert world.pokes == []
    assert world.notices == []
    assert world.announced is None


def test_a_pinned_minor_tag_already_carries_the_newest_patch(world):
    """release.yml publishes a v1.3.1 build as `v1.3` and `v1` too, so an
    install pinned to `v1.3` is *already running* v1.3.1.

    Comparing the full strings would nag that install about an upgrade it has
    had all along, so the comparison is done at the precision the operator
    actually pinned.
    """
    world.publish("v1.3.1")
    result = world.run(KARAKOS_VERSION="v1.3")

    assert result.returncode == CHECKED
    assert world.pokes == [], "a moving minor tag was reported as out of date"


def test_a_behind_install_is_told_which_release_and_how_to_get_it(world):
    result = world.run(KARAKOS_VERSION="v1.2")

    assert result.returncode == CHECKED
    assert len(world.pokes) == 1
    message = world.pokes[0]
    assert "v1.4" in message and "v1.2" in message
    assert "KARAKOS_VERSION=v1.4" in message
    assert "docker compose pull" in message
    assert "releases/tag/v1.4" in message


def test_an_install_pinned_ahead_is_not_told_to_downgrade(world):
    """A pin can legitimately sit ahead of the newest published release — a
    release candidate, or a tag published before the release object was.
    """
    result = world.run(KARAKOS_VERSION="v2.0")

    assert result.returncode == CHECKED
    assert world.pokes == []


@pytest.mark.parametrize("current,latest,behind", [
    ("v1.2", "v1.4", True),
    ("v1.9", "v1.10", True),      # not a string comparison
    ("v1.3", "v1.3.1", False),    # the v1.3 tag carries 1.3.1
    ("v1", "v1.4", False),        # so does the v1 tag
    ("v1.3.1", "v1.3.2", True),
    ("v1.4", "v1.4", False),
    ("v2.0", "v1.4", False),
])
def test_version_comparison(world, current, latest, behind):
    world.publish(latest)
    result = world.run(KARAKOS_VERSION=current)

    assert result.returncode == CHECKED, result.stdout + result.stderr
    assert bool(world.pokes) is behind, result.stdout


# =============================================================================
# 4. The notification — and which channel it takes
# =============================================================================

def test_the_notice_goes_through_the_agent_queue_not_straight_to_discord(world):
    """The opposite call from bin/cli-upgrade-watchdog.sh, for the reason that
    script gives for its own choice.

    The watchdog bypasses poke.sh because the thing it reports is that agents
    cannot complete a turn — a queued notice would land in the one place nobody
    can read it. That condition does not hold here. An available update is not
    an outage: agents are answering normally, and an agent can do something with
    this that a raw webhook post cannot — read the release notes, say what
    actually changed, and be asked "should we?" in the same thread.

    Behavioural, not a grep: a discord-notify.sh sits right next to poke.sh in
    the same directory and must stay untouched.
    """
    world.run(KARAKOS_VERSION="v1.2")

    assert world.pokes, "no notice was sent at all"
    assert world.notices == [], "the notice bypassed the agent and went to the webhook"


def test_the_notice_is_answered_in_signals(world):
    """#signals is where UPGRADING.md tells operators to look for it."""
    world.run(KARAKOS_VERSION="v1.2")

    assert "--reply-channel signals" in world.pokes[0]
    assert "--source update-checker" in world.pokes[0]


def test_the_reply_channel_is_overridable(world):
    world.run("--channel", "general", KARAKOS_VERSION="v1.2")

    assert "--reply-channel general" in world.pokes[0]


# =============================================================================
# 5. Cadence — said once, not every Monday forever
# =============================================================================

def test_a_release_is_announced_once_not_every_week(world):
    """Weekly repetition of a notice nobody has acted on is how a channel gets
    muted, and a muted #signals costs more than this check is worth.
    """
    world.run(KARAKOS_VERSION="v1.2")
    world.run(KARAKOS_VERSION="v1.2")
    world.run(KARAKOS_VERSION="v1.2")

    assert len(world.pokes) == 1
    assert world.announced == "v1.4"


def test_the_next_release_is_announced_again(world):
    """Once-per-release, not once-ever — the state records a tag, not a flag."""
    world.run(KARAKOS_VERSION="v1.2")
    world.publish("v1.5")
    world.run(KARAKOS_VERSION="v1.2")

    assert len(world.pokes) == 2
    assert "v1.5" in world.pokes[1]
    assert world.announced == "v1.5"


def test_force_re_announces_for_a_manual_run(world):
    """UPGRADING.md documents running this by hand. A hand-run that prints
    nothing because of a state file written weeks ago looks like a broken
    script.
    """
    world.run(KARAKOS_VERSION="v1.2")
    world.run("--force", KARAKOS_VERSION="v1.2")

    assert len(world.pokes) == 2


@pytest.mark.parametrize("junk", ["{not json", "[]", "", '{"last_announced": null}'])
def test_a_corrupt_state_file_does_not_suppress_the_notice(world, junk):
    """A truncated state file must degrade to "say it again", never to silence.
    Silence is the failure mode this whole issue is about.
    """
    world.state_file.write_text(junk)
    result = world.run(KARAKOS_VERSION="v1.2")

    assert result.returncode == CHECKED, result.stdout + result.stderr
    assert world.pokes, "a corrupt state file swallowed the notice"


# =============================================================================
# 6. Failing loudly enough for the scheduler to log it
# =============================================================================

def test_an_unreachable_releases_api_exits_nonzero(world):
    """Not a Discord notice — a network blip is not news, and this is a weekly
    job, so paging on it would be noise. It exits non-zero so scheduler.log
    carries it, which is what stops a permanently broken checker from going
    quiet for another month.
    """
    result = world.run(url=f"file://{world.root}/does-not-exist.json")

    assert result.returncode == CANNOT_CHECK
    assert world.pokes == []
    assert world.notices == []
    assert result.stderr.strip()


def test_a_response_with_no_tag_name_exits_nonzero(world):
    """A 200 with an unexpected body — a renamed repo, a rate-limit message —
    is the same class of silent breakage as a dead URL.
    """
    world.publish_raw('{"message": "Not Found"}')
    result = world.run()

    assert result.returncode == CANNOT_CHECK
    assert world.pokes == []


def test_a_notice_that_could_not_be_sent_is_a_failed_run(world):
    """"Found an update, told nobody" is precisely fault #2 of this issue. It
    must not exit 0.
    """
    world.break_poke()
    result = world.run(KARAKOS_VERSION="v1.2")

    assert result.returncode == CANNOT_CHECK
    assert result.stderr.strip()


def test_a_failed_notice_is_not_recorded_as_announced(world):
    """Otherwise the once-per-release rule would suppress the retry, and the
    release would never be announced at all.
    """
    world.break_poke()
    world.run(KARAKOS_VERSION="v1.2")

    assert world.announced is None


def test_a_missing_release_url_falls_back_rather_than_failing(world):
    """The link is a convenience; its absence is not a reason to skip a notice."""
    world.publish("v1.4")          # no html_url
    result = world.run(KARAKOS_VERSION="v1.2")

    assert result.returncode == CHECKED
    assert "releases/tag/v1.4" in world.pokes[0]


# =============================================================================
# 7. Wiring — a check nothing runs, or whose failure nothing logs
# =============================================================================

def _function_node(tree, name):
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def test_the_scheduler_still_runs_the_weekly_check():
    """Parsed rather than grepped: scheduler.py discusses several scripts in
    comments, and a comment is not a call site.
    """
    tree = ast.parse(SCHEDULER.read_text())
    node = _function_node(tree, "check_updates")
    literals = [c.value for c in ast.walk(node)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    assert any("check-updates.sh" in lit for lit in literals)

    main = _function_node(tree, "main")
    weekly = [
        c for c in ast.walk(main)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
        and c.func.attr == "do"
        and c.args and isinstance(c.args[0], ast.Name)
        and c.args[0].id == "check_updates"
    ]
    assert weekly, "the update check is not scheduled at all"


def test_the_scheduler_logs_a_failed_update_check():
    """The other half of #152: the scheduler ran this with capture_output=True
    and `check=True`, so a non-zero exit raised, was caught, and logged only
    `e.stderr` — while the script's own explanation went to stdout and was
    discarded with it. A weekly job that fails invisibly is broken for a month
    before anyone can notice.
    """
    tree = ast.parse(SCHEDULER.read_text())
    node = _function_node(tree, "check_updates")

    returncode_checks = [n for n in ast.walk(node)
                         if isinstance(n, ast.Attribute) and n.attr == "returncode"]
    assert returncode_checks, "the exit code of the update check is never inspected"

    errors = [n for n in ast.walk(node)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "error"]
    assert errors, "a failed update check is never logged"

    # The script explains itself on stdout, so throwing stdout away on failure
    # is how the reason gets lost even once the exit code is noticed.
    body = ast.unparse(node)
    assert "stdout" in body, "the failure is logged without the script's own output"


def test_the_script_is_executable():
    assert CHECK_UPDATES.exists()
    assert CHECK_UPDATES.stat().st_mode & stat.S_IXUSR
