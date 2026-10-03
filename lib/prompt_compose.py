#!/usr/bin/env python3
"""System-prompt composition: core + agent section + shard text + house style.

    from prompt_compose import compose_system_prompt
    text = compose_system_prompt(workspace, agent_id, shard_id=None)

Files (all optional; an absent or unreadable file is skipped with a warning):
    agents/CORE.md                       shared body for every agent
    agents/HOUSE_STYLE.md                fleet-wide style, appended last
    agents/<id>/SYSTEM_PROMPT.md         the agent section (may hold the splice marker)
    agents/<id>/shards/<shard-id>.md     optional per-shard text

Two distinct kinds of marker:
    splice marker   ``<!-- core:insert -->`` on a line by itself: replaced by the
                    wrapped core block, never present in the output.
    wrapper tags    ``<!-- begin:core -->`` ... ``<!-- end:core -->`` and
                    ``<!-- begin:house-style -->`` ... ``<!-- end:house-style -->``
                    delimit the generated blocks. Existing wrapped blocks are
                    stripped before composing, so composing a generated file again
                    is idempotent.

With no CORE.md and no HOUSE_STYLE.md the output is the section text unchanged
(apart from the six known placeholders).
"""

import json
import logging
import os
import re
import sys
from pathlib import Path

log = logging.getLogger(__name__)

SPLICE_RE = re.compile(r"^[ \t]*<!-- core:insert -->[ \t]*$", re.MULTILINE)
WRAPPER_RE = re.compile(r"<!-- begin:(core|house-style) -->.*?<!-- end:\1 -->(\n?)", re.DOTALL)
PLACEHOLDER_RE = re.compile(
    r"\{\{(AGENT_NAME|SYSTEM_NAME|OWNER_NAME|CHANNELS|OTHER_AGENTS|SHARD_ID)\}\}")
_SENTINEL = "\x00karakos-core\x00"


def _read(path, label):
    """File text, or None (with a warning if the file exists but cannot be read)."""
    try:
        if not path.exists():
            return None
        return path.read_text()
    except (OSError, UnicodeError) as exc:
        log.warning("prompt: cannot read %s %s: %s", label, path, exc)
        return None


def _wrap(tag, body):
    return f"<!-- begin:{tag} -->\n{body.strip()}\n<!-- end:{tag} -->"


def _load_registry(workspace):
    try:
        import registry
        return registry.load_registry(workspace)
    except Exception as exc:  # RegistryError, ImportError, bad YAML
        log.warning("prompt: registry unavailable, using defaults: %s", exc)
        return None


def _prompt_settings(agent_id, config, agent):
    """-> (section_rel_path, use_core, use_house_style)."""
    if config is None and agent is not None:
        config = {"prompt": agent.get("prompt"), "system_prompt": agent.get("system_prompt")}
    config = config or {}
    prompt = config.get("prompt")
    if isinstance(prompt, dict):
        return (prompt.get("section") or f"agents/{agent_id}/SYSTEM_PROMPT.md",
                prompt.get("core", True) is not False,
                prompt.get("house_style", True) is not False)
    # Legacy `system_prompt:` maps to the section with both flags on.
    return (config.get("system_prompt") or f"agents/{agent_id}/SYSTEM_PROMPT.md", True, True)


def _owner_system(workspace):
    owner = os.environ.get("OWNER_NAME")
    system = os.environ.get("SYSTEM_NAME")
    if not (owner and system):
        try:
            cfg = json.loads((Path(workspace) / ".karakos" / "config.json").read_text())
            owner = owner or cfg.get("owner_name")
            system = system or cfg.get("system_name")
        except (OSError, ValueError, AttributeError):
            pass
    return owner or "User", system or "karakos"


def _channels_text(workspace, reg):
    try:
        cfg = json.loads((Path(workspace) / "config" / "channels.json").read_text())
        names = list(cfg.get("channels", {}))
    except (OSError, ValueError, AttributeError):
        return "- #general"
    lines = []
    for name in names:
        shard = reg.shard_for_channel(name) if reg else None
        lines.append(f"- #{name}" + (f" (default: {shard.agent})" if shard else ""))
    return "\n".join(lines)


def _other_agents_text(reg, agent_id):
    if not reg:
        return ""
    return "\n".join(f"- **{a.id.title()}** ({a.get('model') or 'sonnet'})"
                     for a in reg.agents() if a.id != agent_id)


def compose_system_prompt(workspace, agent_id, shard_id=None, config=None):
    """Compose the system prompt for ``agent_id`` (shard ``shard_id``; the default
    shard is the agent itself). ``config`` optionally overrides the agent's
    ``prompt`` / ``system_prompt`` settings (a legacy-view dict); by default they
    come from the registry. Never raises over a prompt file."""
    workspace = Path(workspace)
    reg = _load_registry(workspace)
    agent = None
    if reg is not None:
        try:
            agent = reg.agent(agent_id)
        except KeyError:
            agent = None
    shard = shard_id or agent_id
    section_rel, use_core, use_hs = _prompt_settings(agent_id, config, agent)

    section = _read(workspace / section_rel, "section") or ""
    core = _read(workspace / "agents" / "CORE.md", "core") if use_core else None
    shard_text = _read(workspace / "agents" / agent_id / "shards" / f"{shard}.md", "shard")
    house = _read(workspace / "agents" / "HOUSE_STYLE.md", "house style") if use_hs else None
    core = core if core and core.strip() else None
    house = house if house and house.strip() else None

    # Drop previously generated blocks; remember where the core one sat.
    def _strip(m):
        return _SENTINEL + m.group(2) if m.group(1) == "core" else ""
    section = WRAPPER_RE.sub(_strip, section)
    core_block = _wrap("core", core) if core else ""

    if SPLICE_RE.search(section):
        section = section.replace(_SENTINEL, "")
        section = SPLICE_RE.sub(lambda m: core_block, section, count=1)
        section = SPLICE_RE.sub("", section)
    elif _SENTINEL in section:
        first, _, rest = section.partition(_SENTINEL)
        section = first + core_block + rest.replace(_SENTINEL, "")
    elif core_block:
        section = core_block + "\n\n" + section if section else core_block

    result = section
    for part in (shard_text, _wrap("house-style", house) if house else None):
        if part and part.strip():
            result = (result.rstrip("\n") + "\n\n" if result else "") + part

    owner, system = _owner_system(workspace)
    values = {
        "AGENT_NAME": agent.name if agent is not None else agent_id,
        "SYSTEM_NAME": system,
        "OWNER_NAME": owner,
        "CHANNELS": lambda: _channels_text(workspace, reg),
        "OTHER_AGENTS": lambda: _other_agents_text(reg, agent_id),
        "SHARD_ID": shard,
    }

    def _sub(m):
        v = values[m.group(1)]
        return v() if callable(v) else v
    return PLACEHOLDER_RE.sub(_sub, result)


def generated_path(workspace, agent_id, shard_id=None):
    base = Path(workspace) / "agents" / agent_id
    if not shard_id or shard_id == agent_id:
        return base / "SYSTEM_PROMPT.generated.md"
    return base / "shards" / f"{shard_id}.generated.md"


def write_generated(workspace, agent_id, text, shard_id=None):
    """Debug aid: the composed prompt, next to its sources. Never the source."""
    path = generated_path(workspace, agent_id, shard_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    except OSError as exc:
        log.warning("prompt: cannot write %s: %s", path, exc)


if __name__ == "__main__":
    ws, aid = sys.argv[1], sys.argv[2]
    sys.stdout.write(compose_system_prompt(ws, aid, sys.argv[3] if len(sys.argv) > 3 else None))
