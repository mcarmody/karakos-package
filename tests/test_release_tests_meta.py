"""Tests of the release tests (7.3a). Unit job only: no Docker, no real CLI."""

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import realcli_schema as schema
import realcli_support
from harness import FAKE_BIN_DIR, Harness

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / "tests" / "smoke"
WORKFLOWS = ROOT / ".github" / "workflows"


def pytest_collect(*args):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "tests")}
    return subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                           "-p", "no:cacheprovider", *args],
                          cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=300)


# ------------------------------------------------------------------ markers

def test_markers_registered():
    ini = (ROOT / "pytest.ini").read_text()
    for m in ("slow", "docker", "realcli"):
        assert re.search(rf"^\s+{m}:", ini, re.M), m


def test_docker_and_realcli_tests_also_carry_slow():
    out = pytest_collect("tests", "-m", "(docker or realcli) and not slow")
    assert "no tests collected" in out.stdout + out.stderr or out.returncode == 5, out.stdout[-800:]
    both = pytest_collect("tests", "-m", "docker or realcli")
    assert "test_fresh_install_smoke.py" in both.stdout
    assert "test_upgrade_smoke.py" in both.stdout
    assert "test_real_cli_smoke.py" in both.stdout


def test_existing_docker_build_tests_carry_docker():
    out = pytest_collect("tests/test_smoke_docker.py", "-m", "docker")
    ids = [l for l in out.stdout.splitlines() if "TestDockerBuild" in l]
    assert len(ids) == 4, out.stdout


def test_realcli_gate_command_selects_realcli_and_no_docker_tests():
    # Other realcli tests (6.4, 2.5, 6.x) are in the gate too; the point is that
    # the Docker tests are not.
    out = pytest_collect("tests", "-m", "slow and realcli")
    lines = [l for l in out.stdout.splitlines() if "::" in l]
    assert any("test_real_cli_smoke.py" in l for l in lines)
    assert not [l for l in lines if "smoke_docker" in l or "fresh_install" in l or "upgrade_smoke" in l], lines


# ------------------------------------------------------------------ skip vs fail

def run_one_test(tmp_path, body, env_extra, path_dirs):
    f = tmp_path / "test_one.py"
    f.write_text("import pytest\nfrom realcli_support import real_cli, require_docker\n" + body)
    env = {k: v for k, v in os.environ.items()
           if k not in ("KARAKOS_REQUIRE_REAL_CLI", "KARAKOS_REQUIRE_DOCKER",
                        "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")}
    env["PATH"] = os.pathsep.join(str(p) for p in path_dirs)
    env["PYTHONPATH"] = str(ROOT / "tests")
    env.update(env_extra)
    return subprocess.run([sys.executable, "-m", "pytest", str(f), "-q", "-rs", "-p", "no:cacheprovider",
                           "--rootdir", str(tmp_path)],
                          cwd=str(tmp_path), capture_output=True, text=True, env=env, timeout=120)


def fake_bin(tmp_path, name):
    d = tmp_path / f"bin-{name}"
    d.mkdir()
    exe = d / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return d


EMPTY = "def test_it(real_cli):\n    pass\n"


def test_real_cli_skips_without_binary_and_fails_when_required(tmp_path):
    empty = tmp_path / "empty"; empty.mkdir()
    r = run_one_test(tmp_path, EMPTY, {}, [empty])
    assert r.returncode == 0 and "1 skipped" in r.stdout and "no `claude` binary" in r.stdout, r.stdout
    r = run_one_test(tmp_path, EMPTY, {"KARAKOS_REQUIRE_REAL_CLI": "1"}, [empty])
    assert r.returncode != 0 and "no `claude` binary" in r.stdout + r.stderr, r.stdout


def test_real_cli_skips_without_credential_and_fails_when_required(tmp_path):
    claude = fake_bin(tmp_path, "claude")
    r = run_one_test(tmp_path, EMPTY, {}, [claude])
    assert r.returncode == 0 and "1 skipped" in r.stdout and "no credential" in r.stdout, r.stdout
    r = run_one_test(tmp_path, EMPTY, {"KARAKOS_REQUIRE_REAL_CLI": "1"}, [claude])
    assert r.returncode != 0 and "no credential" in r.stdout + r.stderr, r.stdout


def test_real_cli_fixture_isolates_home(tmp_path):
    claude = fake_bin(tmp_path, "claude")
    body = ("def test_it(real_cli, tmp_path):\n"
            "    env = real_cli.env()\n"
            "    assert env['HOME'].startswith(str(tmp_path)) and env['CLAUDE_CONFIG_DIR'].startswith(env['HOME'])\n"
            "    assert env['CLAUDE_CODE_OAUTH_TOKEN'] == 'tok'\n"
            "    assert real_cli.flags() == ['--model', 'haiku', '--max-budget-usd', '0.25']\n")
    r = run_one_test(tmp_path, body, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}, [claude])
    assert r.returncode == 0, r.stdout + r.stderr


DOCKER = "def test_it():\n    require_docker()\n"


def test_docker_skips_and_fails_when_required(tmp_path):
    empty = tmp_path / "empty"; empty.mkdir()
    r = run_one_test(tmp_path, DOCKER, {}, [empty])
    assert r.returncode == 0 and "1 skipped" in r.stdout and "no `docker` binary" in r.stdout, r.stdout
    r = run_one_test(tmp_path, DOCKER, {"KARAKOS_REQUIRE_DOCKER": "1"}, [empty])
    assert r.returncode != 0 and "no `docker` binary" in r.stdout + r.stderr, r.stdout


def test_spend_ceiling(monkeypatch):
    c = realcli_support.SpendCounter(0.50)
    assert c.add(0.30) == pytest.approx(0.30)
    with pytest.raises(realcli_support.BudgetExceeded, match="KARAKOS_REALCLI_BUDGET_USD"):
        c.add(0.30)
    monkeypatch.setenv("KARAKOS_REALCLI_BUDGET_USD", "0.10")
    assert realcli_support.SpendCounter().ceiling == pytest.approx(0.10)
    monkeypatch.delenv("KARAKOS_REALCLI_BUDGET_USD")
    assert realcli_support.SpendCounter().ceiling == pytest.approx(0.50)


# ------------------------------------------------------------------ the schema and shape helpers

REAL = ROOT / "tests" / "harness" / "fixtures" / "real-cli"


def test_schema_canary_accepts_the_recorded_success_turn():
    ev = schema.fixture_events(REAL / "5-resume-system-prompt" / "stdout.jsonl")
    assert schema.check_events(ev) == []


def test_schema_canary_names_a_missing_key_and_prints_the_event():
    ev = schema.fixture_events(REAL / "5-resume-system-prompt" / "stdout.jsonl")
    for e in ev:
        if e.get("type") == "result":
            e.pop("total_cost_usd")
    problems = schema.check_events(ev)
    assert problems and "total_cost_usd" in problems[0] and "result" in problems[0]


def test_schema_keys_are_the_ones_read_events_reads():
    src = (ROOT / "lib" / "turn_loop.py").read_text()
    for key in ("total_cost_usd", "duration_ms", "is_error", "input_tokens", "output_tokens",
                "parent_tool_use_id", "session_id"):
        assert key in src, key


def test_shape_drops_noise_and_collapses_repeats():
    for q, want in {"3-interrupt": [("system", "init"), ("user", None), ("assistant", None),
                                    ("control_response", "success"), ("user", None),
                                    ("result", "error_during_execution"), ("system", "init"),
                                    ("user", None), ("assistant", None), ("result", "success")],
                    "5-resume-system-prompt": [("system", "init"), ("assistant", None),
                                               ("result", "success")]}.items():
        ev = schema.fixture_events(REAL / q / "stdout.jsonl")
        assert schema.shape(ev) == want, q


def test_realcli_smoke_names_the_recorded_cli_version():
    text = (REAL / "SUMMARY.md").read_text()
    assert re.search(r"Claude Code \d+\.\d+\.\d+", text)


# ------------------------------------------------------------------ safety guards

SCRIPTS = ["fresh_install.sh", "upgrade.sh"]


def _fake_docker(tmp_path):
    d = tmp_path / "fakebin"
    d.mkdir()
    log = tmp_path / "docker-argv.log"
    exe = d / "docker"
    exe.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexit 0\n')
    exe.chmod(0o755)
    return d, log


@pytest.mark.parametrize("script", SCRIPTS)
def test_scripts_refuse_a_bad_project_name(tmp_path, script):
    fake, log = _fake_docker(tmp_path)
    work = tmp_path / "work"; work.mkdir()
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "SMOKE_PROJECT": "karakos-prod",
           "SMOKE_WORK": str(work), "TMPDIR": str(tmp_path)}
    r = subprocess.run(["bash", str(SMOKE / script), "v1.5.0"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 97 and "does not match" in r.stderr, (r.returncode, r.stderr)
    assert not log.exists() or "down" not in log.read_text()


@pytest.mark.parametrize("script", SCRIPTS)
def test_scripts_refuse_a_non_temp_work_dir(tmp_path, script):
    fake, log = _fake_docker(tmp_path)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "SMOKE_PROJECT": "karakos-smoke-abcd1234",
           "SMOKE_WORK": str(ROOT), "TMPDIR": str(tmp_path)}
    r = subprocess.run(["bash", str(SMOKE / script), "v1.5.0"], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 97 and "not under the system temp" in r.stderr, (r.returncode, r.stderr)
    assert not log.exists() or "down" not in log.read_text()


def _guard(project, work, tmpdir):
    return subprocess.run(
        ["bash", "-c", f'. "{SMOKE}/guard.sh"; smoke_guard; echo passed'],
        env={**os.environ, "SMOKE_PROJECT": project, "SMOKE_WORK": str(work), "TMPDIR": str(tmpdir)},
        capture_output=True, text=True)


def test_guard_function_accepts_only_the_expected_shape(tmp_path):
    work = tmp_path / "w"; work.mkdir()
    assert _guard("karakos-smoke-abcd1234", work, tmp_path).stdout.strip() == "passed"
    assert _guard("karakos-up-v1-5-0-abcd1234", work, tmp_path).stdout.strip() == "passed"
    for bad in ("karakos", "karakos-prod", "xkarakos-smoke-1", "", "karakos-smokey"):
        r = _guard(bad, work, tmp_path)
        assert r.returncode == 97 and "passed" not in r.stdout, bad
    assert _guard("karakos-smoke-1", tmp_path / "missing", tmp_path).returncode == 97
    assert _guard("karakos-smoke-1", ROOT, tmp_path).returncode == 97
    # the temp dir itself is not a work dir (must be a child)
    assert _guard("karakos-smoke-1", tmp_path, tmp_path).returncode == 97


def test_every_down_v_follows_the_guard_call():
    for script in sorted(SMOKE.glob("*.sh")):
        lines = [l for l in script.read_text().splitlines() if not l.lstrip().startswith("#")]
        seen_guard = False
        found = 0
        for line in lines:
            if re.search(r"\bsmoke_guard\b", line) and "smoke_guard()" not in line:
                seen_guard = True
            if re.search(r"\bdown\s+-v\b|\bdown\b.*\s-v\b|volume\s+rm", line):
                found += 1
                assert seen_guard, f"{script.name}: `{line.strip()}` before any smoke_guard call"
        if script.name != "guard.sh":
            assert found, f"{script.name} never tears down its project"


def test_scripts_never_read_home_or_real_paths():
    for name in SCRIPTS:
        text = (SMOKE / name).read_text()
        assert 'export HOME="$SMOKE_WORK/home"' in text
        assert "~/.claude" not in text and "$HOME/.claude" in text   # only the temp HOME's


# ------------------------------------------------------------------ scripts

def test_scripts_pass_bash_n():
    scripts = sorted(SMOKE.glob("*.sh"))
    assert {p.name for p in scripts} >= {"fresh_install.sh", "upgrade.sh", "guard.sh"}
    for s in scripts:
        r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
        assert r.returncode == 0, (s, r.stderr)


def test_scripts_pass_shellcheck():
    if not shutil.which("shellcheck"):
        pytest.skip("shellcheck is not installed")
    r = subprocess.run(["shellcheck", "-S", "warning", "-x", *map(str, sorted(SMOKE.glob("*.sh")))],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_make_config_output_validates_and_has_entrypoint_vars(tmp_path):
    sys.path.insert(0, str(SMOKE))
    try:
        import make_config
    finally:
        sys.path.pop(0)
    values = make_config.write_install(tmp_path, 3101, 18901, "/usr/bin/claude")
    env = dict(l.split("=", 1) for l in (tmp_path / "config" / ".env").read_text().splitlines())
    for var in make_config.entrypoint_required_vars():
        assert env.get(var), var
    assert env["DASHBOARD_PORT"] == "3101" and env["AGENT_SERVER_PORT"] == "18901"
    assert values["AGENT_SERVER_TOKEN"] == env["AGENT_SERVER_TOKEN"]
    reg = yaml.safe_load((tmp_path / "config" / "agents.yaml").read_text())
    roles = {a["role"] for a in reg["agents"].values()}
    assert {"primary", "monitor"} <= roles
    assert "${FAKE_CLAUDE_SCRIPT}" in json.dumps(reg)
    override = (tmp_path / "config" / "docker-compose.smoke.yml").read_text()
    assert "/opt/fake:ro" in override and ":/usr/bin/claude:ro" in override
    assert json.loads((tmp_path / "config" / "channels.json").read_text())["channels"]
    # the registry validates through the shipped CLI
    r = subprocess.run([sys.executable, str(ROOT / "lib" / "registry.py"), "--workspace",
                        str(tmp_path), "validate"], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stdout + r.stderr


def test_make_config_parity_guard_trips_on_a_missing_variable(tmp_path):
    sys.path.insert(0, str(SMOKE))
    try:
        import make_config
    finally:
        sys.path.pop(0)
    values = make_config.write_install(tmp_path, 3101, 18901, "/usr/bin/claude")
    values.pop("AGENT_SERVER_TOKEN")
    with pytest.raises(SystemExit, match="AGENT_SERVER_TOKEN"):
        make_config.validate(tmp_path, values)


# ------------------------------------------------------------------ workflows

def load(name):
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    doc["_on"] = doc.get("on", doc.get(True))      # PyYAML reads a bare `on` as True
    return doc


def test_release_gate_triggers():
    on = load("release-gate.yml")["_on"]
    assert set(on) == {"workflow_call", "workflow_dispatch", "schedule", "pull_request"}
    assert "push" not in on
    assert on["pull_request"].get("paths")
    for p in ("Dockerfile", "bin/entrypoint.sh", "lib/migrate/**", "tests/smoke/**",
              ".github/workflows/release-gate.yml"):
        assert p in on["pull_request"]["paths"], p
    crons = on["schedule"]
    assert len(crons) == 1
    minute, hour, dom, month, dow = crons[0]["cron"].split()
    assert dom == "*" and month == "*" and dow != "*", "must be weekly, no more often"


def test_every_job_has_a_timeout():
    for name in ("release-gate.yml", "ci.yml"):
        for job, spec in load(name)["jobs"].items():
            if "uses" in spec:
                continue
            assert "timeout-minutes" in spec or name == "ci.yml", (name, job)
    gate = load("release-gate.yml")["jobs"]
    assert set(gate) >= {"build-image", "fresh-install-smoke", "upgrade-smoke", "real-cli-smoke"}
    assert gate["real-cli-smoke"]["timeout-minutes"] and gate["real-cli-smoke"]["env"]
    assert gate["upgrade-smoke"]["strategy"]["matrix"]["tag"] == ["v1.3", "v1.5.0"]
    assert gate["fresh-install-smoke"]["needs"] == "build-image"
    assert gate["upgrade-smoke"]["needs"] == "build-image"


def test_release_gate_missing_secrets_behave_as_stated():
    gate = load("release-gate.yml")["jobs"]
    # The dashboard is in-repo: no secret, so the image build and both Docker smokes never skip.
    for name in ("build-image", "fresh-install-smoke", "upgrade-smoke"):
        assert "if" not in gate[name], name
    for wf in WORKFLOWS.glob("*.yml"):
        assert "DASHBOARD_FETCH_TOKEN" not in wf.read_text(), wf.name
    # CLAUDE_CODE_OAUTH_TOKEN absent: skip on a PR, fail otherwise
    real = json.dumps(gate["real-cli-smoke"])
    assert "= pull_request" in real and "exit 1" in real
    text = (WORKFLOWS / "release-gate.yml").read_text()
    assert "KARAKOS_REQUIRE_REAL_CLI" in text and "KARAKOS_REQUIRE_DOCKER" in text


def test_release_build_push_needs_the_gate():
    rel = load("release.yml")["jobs"]
    assert rel["gate"]["uses"] == "./.github/workflows/release-gate.yml"
    assert rel["gate"]["secrets"] == "inherit"
    needs = rel["build-push"]["needs"]
    assert "gate" in ([needs] if isinstance(needs, str) else needs)


def test_ci_real_cli_job_is_gated_and_there_is_one_schedule():
    ci = load("ci.yml")
    cond = ci["jobs"]["real-cli-smoke"]["if"]
    assert "schedule" in cond and "workflow_dispatch" in cond and "pull_request" not in cond
    assert len(ci["_on"]["schedule"]) == 1
    assert "push" not in ci["_on"]
    steps = json.dumps(ci["jobs"]["real-cli-smoke"])
    assert "KARAKOS_REQUIRE_REAL_CLI" in steps and "CLAUDE_CODE_OAUTH_TOKEN" in steps
    assert 'slow and realcli' in steps


# ------------------------------------------------------------------ harness keyword

class _StubApp:
    pass


def _stub_import(*_a, **_k):
    from aiohttp import web

    class Module:
        post_to_discord = None

        @staticmethod
        def create_app():
            return web.Application()
    return Module


def _start_and_path(tmp_workspace, **kw):
    h = Harness(tmp_workspace, agents=["a"], **kw)
    h._import_script = _stub_import
    before = os.environ["PATH"]
    seen = {}

    async def go():
        await h.start()
        seen["path"] = os.environ["PATH"]
        await h.stop()
    asyncio.run(go())
    assert os.environ["PATH"] == before      # stop() restores it
    return before, seen["path"]


def test_harness_claude_default_is_fake_and_prepends_the_fake(tmp_workspace):
    assert Harness.__init__.__defaults__[-1] == "fake"
    before, during = _start_and_path(tmp_workspace)
    assert during.split(os.pathsep)[0] == str(FAKE_BIN_DIR)


def test_harness_claude_real_does_not_prepend_the_fake(tmp_workspace, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    before, during = _start_and_path(tmp_workspace, claude="real")
    assert during == before and str(FAKE_BIN_DIR) not in during.split(os.pathsep)
    reg = yaml.safe_load((Path(tmp_workspace) / "config" / "agents.yaml").read_text())
    assert reg["agents"]["a"]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "${CLAUDE_CODE_OAUTH_TOKEN}"
    assert reg["agents"]["a"]["model"] == "haiku"


def test_harness_claude_rejects_other_values(tmp_workspace):
    with pytest.raises(ValueError):
        Harness(tmp_workspace, agents=["a"], claude="bogus")


# ------------------------------------------------------------------ smoke tag parity (with 7.2)

def test_quickstart_smoke_lines_are_in_fresh_install_script():
    from test_docs import _smoke_lines
    body = (SMOKE / "fresh_install.sh").read_text()
    lines = _smoke_lines()
    assert lines
    missing = [l for l in lines if l not in body]
    assert not missing, missing


def test_compose_up_never_pulls_and_leftovers_are_checked():
    for name in SCRIPTS:
        text = (SMOKE / name).read_text()
        for line in text.splitlines():
            if re.search(r"\bup -d\b", line) and not line.lstrip().startswith("#"):
                assert "--pull never" in line, f"{name}: {line}"
        assert "smoke_assert_clean" in text
