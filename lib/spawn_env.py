"""Environment for `claude` subprocesses: an allowlist, not a copy of ours.

The agent-server's own environment holds every Discord token and API key.
Handing all of it to each claude subprocess (and to every hook, MCP server and
shell command those run) is needless blast radius. Build the child env from
nothing: inert names from the server env, then the agent's registry `env:`
(with `${NAME}` references resolved from the server env), then the server's
own identity vars.
"""

import logging
import re
from typing import Mapping

log = logging.getLogger("spawn_env")

# Exact names copied through from the server environment.
INERT_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LANG", "TZ", "TERM", "TMPDIR", "SHELL", "PWD",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
    # What the claude CLI needs to authenticate and find its config.
    "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
})
# Prefix families copied through.
INERT_ENV_PREFIXES = ("LC_", "XDG_")

_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _allowed(name: str) -> bool:
    return name in INERT_ENV_ALLOWLIST or name.startswith(INERT_ENV_PREFIXES)


def resolve_agent_env(agent_env: Mapping, base: Mapping, label: str = "") -> dict:
    """Resolve `${NAME}` values from `base`. Unresolved: warn, omit the key."""
    out = {}
    for key, value in (agent_env or {}).items():
        value = "" if value is None else str(value)
        m = _REF.match(value)
        if m:
            ref = m.group(1)
            if ref not in base:
                log.warning("%s env %s references ${%s}, which is not set in the "
                            "server environment; omitting", label or "agent", key, ref)
                continue
            value = base[ref]
        out[str(key)] = value
    return out


def build_subprocess_env(base: Mapping, agent_env: Mapping, extra: Mapping) -> dict:
    """Allowlisted base, then agent_env (references resolved), then extra."""
    env = {k: v for k, v in base.items() if _allowed(k)}
    env.update(resolve_agent_env(agent_env, base))
    env.update(extra)
    return env


def passthrough_requested(base: Mapping) -> bool:
    """KARAKOS_ENV_PASSTHROUGH=1 restores the old inherit-everything behaviour,
    except in production, where it is refused (with a warning)."""
    if base.get("KARAKOS_ENV_PASSTHROUGH", "") not in ("1", "true", "True", "yes"):
        return False
    if base.get("KARAKOS_ENV", "") == "production":
        log.warning("KARAKOS_ENV_PASSTHROUGH is refused when KARAKOS_ENV=production; "
                    "using the allowlist")
        return False
    log.warning("KARAKOS_ENV_PASSTHROUGH=1: subprocesses inherit the server's full "
                "environment, including every token. Debugging only.")
    return True
