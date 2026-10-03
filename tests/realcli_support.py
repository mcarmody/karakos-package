"""Support for tests that call the real `claude` CLI (7.3a).

`real_cli` is a fixture (registered through tests/conftest.py's import of this
module's fixtures). It finds `claude` on PATH and a credential in the
environment (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY). Missing either:
skip with the reason, unless KARAKOS_REQUIRE_REAL_CLI=1, which fails instead,
so a gate job with no credential cannot pass by testing nothing.

The child environment is built from spawn_env.build_subprocess_env plus the
credential, with HOME and CLAUDE_CONFIG_DIR pointed at a temporary directory
(no developer account state is read; tests/test_no_home_access.py holds).
Spend is bounded: every call passes --max-budget-usd 0.25 and a session-wide
counter fails the run past KARAKOS_REALCLI_BUDGET_USD (default 0.50).
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CREDENTIAL_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
PER_CALL_BUDGET_USD = "0.25"
DEFAULT_SESSION_BUDGET_USD = 0.50
MODEL = "haiku"


class BudgetExceeded(AssertionError):
    pass


class SpendCounter:
    """Sums total_cost_usd across a session; fails past the ceiling."""

    def __init__(self, ceiling=None):
        if ceiling is None:
            raw = os.environ.get("KARAKOS_REALCLI_BUDGET_USD")
            ceiling = float(raw) if raw else DEFAULT_SESSION_BUDGET_USD
        self.ceiling = ceiling
        self.total = 0.0

    def add(self, cost):
        self.total += float(cost or 0.0)
        if self.total > self.ceiling:
            raise BudgetExceeded(
                f"real-CLI spend ${self.total:.4f} exceeds KARAKOS_REALCLI_BUDGET_USD "
                f"${self.ceiling:.2f}")
        return self.total


def required(flag: str) -> bool:
    return os.environ.get(flag, "") == "1"


def skip_or_fail(flag: str, reason: str):
    """pytest.fail when `flag` is 1 (a gate), else pytest.skip with the reason."""
    if required(flag):
        pytest.fail(f"{flag}=1 and {reason}", pytrace=False)
    pytest.skip(reason)


def require_docker():
    """Skip (or fail under KARAKOS_REQUIRE_DOCKER=1) when there is no docker binary."""
    if not shutil.which("docker"):
        skip_or_fail("KARAKOS_REQUIRE_DOCKER", "no `docker` binary on PATH")


class RealCli:
    def __init__(self, path, credential, home, spend):
        self.path = path
        self.credential = credential          # {NAME: value}
        self.home = Path(home)
        self.spend = spend

    def env(self):
        sys.path.insert(0, str(ROOT / "lib"))
        try:
            import spawn_env
        finally:
            sys.path.pop(0)
        base = dict(os.environ)
        env = spawn_env.build_subprocess_env(base, {}, {})
        env.update(self.credential)
        env["HOME"] = str(self.home)
        env["CLAUDE_CONFIG_DIR"] = str(self.home / ".claude")
        return env

    def flags(self):
        """Flags every real-CLI call carries (model and per-call budget)."""
        return ["--model", MODEL, "--max-budget-usd", PER_CALL_BUDGET_USD]


_SESSION_SPEND = None


def session_spend() -> SpendCounter:
    global _SESSION_SPEND
    if _SESSION_SPEND is None:
        _SESSION_SPEND = SpendCounter()
    return _SESSION_SPEND


@pytest.fixture
def real_cli(tmp_path):
    path = shutil.which("claude")
    if not path:
        skip_or_fail("KARAKOS_REQUIRE_REAL_CLI", "no `claude` binary on PATH")
    cred = {k: os.environ[k] for k in CREDENTIAL_VARS if os.environ.get(k)}
    if not cred:
        skip_or_fail("KARAKOS_REQUIRE_REAL_CLI",
                     "no credential (CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY) in the environment")
    home = tmp_path / "realcli-home"
    (home / ".claude").mkdir(parents=True)
    return RealCli(path, cred, home, session_spend())
