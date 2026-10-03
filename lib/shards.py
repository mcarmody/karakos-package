#!/usr/bin/env python3
"""Shard planning: pure functions, stdlib only (step 2.1).

A shard is one `claude` stream-json subprocess of an agent. Every runtime key in
agent-server (queue rows, sessions, cost rows, locks, states) is the *shard id*;
an agent's default shard has the agent's own id, so an install with no
``shards:`` in its registry behaves exactly as it did in 1.x.

Callers pass objects exposing ``.id``, ``.agent`` and ``.channels`` (the
registry's ``Shard``); nothing here imports the registry.
"""

from collections import namedtuple
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class ShardSpec:
    id: str
    agent: str
    channels: tuple = ()
    is_default: bool = True

    def __post_init__(self):
        object.__setattr__(self, "channels", tuple(self.channels))


ShardDiff = namedtuple("ShardDiff", "added removed kept")


def plan_shards(registry) -> List[ShardSpec]:
    """Agents in registry order, each agent's shards in declared order. An agent
    with no declared shards yields its one default shard."""
    specs: List[ShardSpec] = []
    for agent in registry.ids():
        declared = list(registry.shards_of(agent))
        if not declared:
            specs.append(ShardSpec(agent, agent, (), True))
            continue
        for s in declared:
            specs.append(ShardSpec(s.id, s.agent, tuple(s.channels), s.id == s.agent))
    return specs


def diff_shards(old: Sequence[ShardSpec], new: Sequence[ShardSpec]) -> ShardDiff:
    """By shard id. A kept shard whose channels changed is still ``kept``:
    routing is not a respawn reason."""
    old_ids = {s.id for s in old}
    new_ids = {s.id for s in new}
    return ShardDiff(
        added=[s for s in new if s.id not in old_ids],
        removed=[s for s in old if s.id not in new_ids],
        kept=[s for s in new if s.id in old_ids],
    )


def resolve_targets(specs: Sequence[ShardSpec], name: str,
                    shard: Optional[str] = None) -> List[str]:
    """The shard ids an HTTP path ``{name}`` means ([] means 404).

    ``shard`` (the ``?shard=`` value) wins; it must belong to ``name``'s agent
    or equal ``name``. Otherwise an agent id means all its shards in order, and
    a bare shard id means itself."""
    by_id = {s.id: s for s in specs}
    agent_ids = {s.agent for s in specs}
    if shard:
        s = by_id.get(shard)
        if s is None:
            return []
        if shard == name or s.agent == name:
            return [shard]
        return []
    if name in agent_ids:
        return [s.id for s in specs if s.agent == name]
    if name in by_id:
        return [name]
    return []


def first_shard(specs: Sequence[ShardSpec], name: str) -> Optional[str]:
    """A shard id selects itself; an agent id selects the agent's first shard."""
    ids = {s.id for s in specs}
    if name in ids:
        return name
    for s in specs:
        if s.agent == name:
            return s.id
    return None


def aggregate_state(states: Iterable[str]) -> str:
    states = list(states)
    if not states:
        return "UNKNOWN"
    if any(s == "PROCESSING" for s in states):
        return "PROCESSING"
    if any(s == "ERROR_RECOVERY" for s in states):
        return "ERROR_RECOVERY"
    if all(s == "IDLE" for s in states):
        return "IDLE"
    return states[0] if len(states) == 1 else "UNKNOWN"


def shard_label(spec: ShardSpec) -> str:
    return spec.agent if spec.is_default else f"{spec.agent} ({spec.id})"


def orphan_key_warnings(specs: Sequence[ShardSpec], session_keys: Iterable[str],
                        queue_keys: Iterable[str]) -> List[str]:
    """One line per agent with rows under its own id in sessions/message_queue
    that declares shards, none of which has that id."""
    keys = set(session_keys) | set(queue_keys)
    out = []
    seen = set()
    for s in specs:
        if s.agent in seen:
            continue
        seen.add(s.agent)
        ids = {x.id for x in specs if x.agent == s.agent}
        if s.agent in keys and s.agent not in ids:
            out.append(f"agent {s.agent} has data under key {s.agent} but no shard "
                       f"with that id; its history will not be used")
    return out
