"""Relay routing policy: which shard gets a Discord message.

Pure and stdlib only. The relay supplies the registry, the channel's name
(None when the channel is not in channels.json), the mentioned agent id (if
any) and whether the author is a bot; this module decides.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True, eq=False)
class Route:
    shard: str
    agent: str
    reason: str  # "mention" | "channel" | "primary_fallback"

    # A Route compares equal to its agent id string, so a caller that only
    # cares about the agent (and the 1.x-era relay tests) keep working.
    def __eq__(self, other):
        if isinstance(other, Route):
            return (self.shard, self.agent, self.reason) == (
                other.shard, other.agent, other.reason)
        if isinstance(other, str):
            return self.agent == other
        return NotImplemented

    def __hash__(self):
        return hash((self.shard, self.agent, self.reason))


def route_message(registry, channel_name, mentioned_agent, is_bot,
                  channel_opt_out=False) -> Optional[Route]:
    # 1. Not in channels.json: never routed, mention or not (B24).
    if channel_name is None:
        return None

    # 2. A mention of a known agent.
    if mentioned_agent and mentioned_agent in registry.ids():
        owner = registry.shard_for_channel(channel_name)
        if owner is not None and owner.agent == mentioned_agent:
            return Route(owner.id, owner.agent, "mention")
        theirs = registry.shards_of(mentioned_agent)
        if theirs:
            return Route(theirs[0].id, theirs[0].agent, "mention")

    # 3. A bot never routes on a channel default.
    if is_bot:
        return None

    # 4. The shard that owns the channel.
    owner = registry.shard_for_channel(channel_name)
    if owner is not None:
        return Route(owner.id, owner.agent, "channel")

    # 5. Unowned channel: the primary agent's first shard.
    if not channel_opt_out:
        try:
            primary = registry.primary()
            first = registry.shards_of(primary.id)
        except Exception:
            return None
        if first:
            return Route(first[0].id, first[0].agent, "primary_fallback")

    # 6.
    return None
