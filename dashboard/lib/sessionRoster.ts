/**
 * Pure shaping of the /api/agents roster into the /chat session picker's
 * list. Pulled out of ChatSurface so it's testable without React/jsdom.
 */

export interface RosterAgent {
  name: string;
  host?: string;
  state?: string;
  subprocess_alive?: boolean;
  label?: string;
  /** Registry role: primary, monitor, builder, reviewer, custom. */
  role?: string;
}

export interface SessionOption {
  name: string;
  /** Human-friendly name for the picker row. */
  displayName: string;
  host: string;
  live: boolean;
  /** True for the registry's primary agent. */
  primary: boolean;
  /** The agent's own note/definition, when the registry has one. */
  definition?: string;
}

export function friendlyName(name: string): string {
  return name.charAt(0).toUpperCase() + name.slice(1);
}

function isLive(agent: RosterAgent): boolean {
  if (agent.subprocess_alive === false) return false;
  return agent.state !== "DOWN" && agent.state !== "UNKNOWN";
}

/** Build the picker's session list from the raw roster. The primary agent
 * sorts first (it's the default selection); everything else is alphabetical
 * by display name. */
export function buildSessionList(agents: RosterAgent[]): SessionOption[] {
  const options = agents.map((a) => ({
    name: a.name,
    displayName: friendlyName(a.name),
    host: a.host || "local",
    live: isLive(a),
    primary: a.role === "primary",
    definition: a.label,
  }));
  options.sort((a, b) => {
    if (a.primary !== b.primary) return a.primary ? -1 : 1;
    return a.displayName.localeCompare(b.displayName);
  });
  return options;
}

/** The session a fresh, unpinned /chat load should select: the primary agent
 * if there is one (live or not — better to show it selected and idle than
 * silently fall back), else the first option. "" when the roster is empty. */
export function defaultSessionName(options: SessionOption[]): string {
  return (options.find((o) => o.primary) ?? options[0])?.name ?? "";
}
