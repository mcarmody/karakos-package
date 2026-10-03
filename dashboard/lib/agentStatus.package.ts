/**
 * Package implementation of the agent-roster adapter (lib/agentStatus.ts).
 *
 * Sources, and nothing else (docs/package-backend-contract.md):
 *   GET {AGENT_SERVER_URL}/agents   roster, model, label, context_tokens, shards
 *   GET {AGENT_SERVER_URL}/health   liveness, queue depth, session id (best effort)
 *   config/agents.yaml              role, label, dashboard_chat, shard ids + channels
 *                                   (path: KARAKOS_REGISTRY_PATH)
 *
 * No shards.json, no ssh/systemctl, no pty, no queue broker: this file imports
 * none of them and is the only thing a package build loads. The registry is
 * read here, never written (the migrator owns every mutation).
 */

import { readFileSync } from "fs";
import { join } from "path";
import { parse as parseYaml } from "yaml";
import { agentFetch } from "@/lib/api";
import type { AgentRosterRow, AgentShardRow, AgentStatusAdapter, PausedInfo } from "@/lib/agentStatus";

const FETCH_TIMEOUT_MS = 5000;
const STATUS_TTL_MS = 5000;

/** The one host a package install has. */
export const PACKAGE_HOST = "local";

export function registryPath(env: NodeJS.ProcessEnv = process.env): string {
  return env.KARAKOS_REGISTRY_PATH || join(env.WORKSPACE_ROOT || "/workspace", "config", "agents.yaml");
}

/** What the dashboard keeps from one agents.yaml entry. */
export interface RegistryAgent {
  id: string;
  name: string;
  role: string | null;
  label: string | null;
  dashboard_chat: boolean;
  model: string | null;
  /** shard id -> channel names. A missing `shards:` key means one shard named
   * after the agent, with no channels (lib/registry.py). */
  shards: Record<string, string[]>;
}

/** Parse config/agents.yaml (schema version 2). Lenient on purpose: the
 * server validates and refuses a bad file, the dashboard only reads what it
 * can and ignores the rest. Returns [] for an unreadable or malformed file. */
export function parseRegistry(text: string): RegistryAgent[] {
  let data: unknown;
  try {
    data = parseYaml(text);
  } catch {
    return [];
  }
  const raw = (data as { agents?: unknown } | null)?.agents;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  const out: RegistryAgent[] = [];
  for (const [id, body] of Object.entries(raw as Record<string, unknown>)) {
    if (!body || typeof body !== "object") continue;
    const b = body as Record<string, unknown>;
    const shards: Record<string, string[]> = {};
    if (Array.isArray(b.shards) && b.shards.length) {
      for (const s of b.shards) {
        const sd = s as { id?: unknown; channels?: unknown } | null;
        if (sd && typeof sd.id === "string") {
          shards[sd.id] = Array.isArray(sd.channels) ? sd.channels.filter((c): c is string => typeof c === "string") : [];
        }
      }
    } else {
      shards[id] = [];
    }
    out.push({
      id,
      name: typeof b.name === "string" && b.name ? b.name : id,
      role: typeof b.role === "string" ? b.role : null,
      label: typeof b.label === "string" && b.label ? b.label : null,
      dashboard_chat: b.dashboard_chat !== false,
      model: typeof b.model === "string" ? b.model : null,
      shards,
    });
  }
  return out;
}

export function readRegistry(path: string = registryPath()): RegistryAgent[] {
  try {
    return parseRegistry(readFileSync(path, "utf-8"));
  } catch {
    return [];
  }
}

/** One entry of the 2.1 `shards` list; every field optional so a partial row
 * still normalises. */
export type ServerShardRow = Partial<AgentShardRow> & { id: string };

interface ServerAgent {
  name: string;
  context_tokens?: number;
  /** A list from agent-server 2.1+, a `{shard_id: {context_tokens}}` dict from 1.5. */
  shards?: ServerShardRow[] | Record<string, { context_tokens?: number }>;
  model?: string | null;
  state?: string;
  dashboard_chat?: boolean;
  label?: string;
}

interface HealthAgent {
  state?: string;
  alive?: boolean;
  queue_depth?: number;
  session_id?: string;
  context_tokens?: number;
}

function defaultShardRow(id: string): AgentShardRow {
  return {
    id,
    is_default: false,
    state: "UNKNOWN",
    alive: false,
    pid: null,
    session_id: "",
    queue_depth: 0,
    context_tokens: 0,
    channels: [],
    last_channel: null,
    paused: null,
  };
}

function normalizePaused(raw: unknown): PausedInfo | null {
  if (!raw || typeof raw !== "object") return null;
  const p = raw as { reason?: unknown; until?: unknown };
  return {
    reason: typeof p.reason === "string" && p.reason ? p.reason : "unknown",
    until: typeof p.until === "number" && Number.isFinite(p.until) ? p.until : null,
  };
}

/** Normalise the server's `shards` into rows with every field present. An
 * array maps row by row (unknown keys pass through); a legacy dict (a pre-2.1
 * server) becomes one row per key with the other fields defaulted; anything
 * else is []. */
export function normalizeShards(raw: unknown): AgentShardRow[] {
  if (Array.isArray(raw)) {
    const out: AgentShardRow[] = [];
    for (const item of raw) {
      if (!item || typeof item !== "object") continue;
      const row = item as Record<string, unknown>;
      if (typeof row.id !== "string" || !row.id) continue;
      const base = defaultShardRow(row.id);
      const merged: AgentShardRow = { ...base, ...row, id: row.id, paused: normalizePaused(row.paused) };
      // A null/undefined/mistyped field falls back to its default.
      if (typeof merged.state !== "string") merged.state = base.state;
      if (typeof merged.alive !== "boolean") merged.alive = base.alive;
      if (typeof merged.pid !== "number") merged.pid = null;
      if (typeof merged.session_id !== "string") merged.session_id = "";
      if (typeof merged.queue_depth !== "number") merged.queue_depth = 0;
      if (typeof merged.context_tokens !== "number") merged.context_tokens = 0;
      if (typeof merged.is_default !== "boolean") merged.is_default = false;
      merged.channels = Array.isArray(row.channels) ? row.channels.filter((c): c is string => typeof c === "string") : [];
      if (typeof merged.last_channel !== "string") merged.last_channel = null;
      out.push(merged);
    }
    return out;
  }
  if (raw && typeof raw === "object") {
    return Object.entries(raw as Record<string, unknown>).map(([id, info]) => {
      const row = defaultShardRow(id);
      const ctx = (info as { context_tokens?: unknown } | null)?.context_tokens;
      if (typeof ctx === "number") row.context_tokens = ctx;
      return row;
    });
  }
  return [];
}

let cache: { at: number; agents: ServerAgent[]; health: Record<string, HealthAgent> } | null = null;

async function fetchServer(): Promise<{ agents: ServerAgent[]; health: Record<string, HealthAgent> }> {
  if (cache && Date.now() - cache.at < STATUS_TTL_MS) return cache;
  const [agentsRes, healthRes] = await Promise.allSettled([
    agentFetch("/agents", { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS), cache: "no-store" }),
    agentFetch("/health", { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS), cache: "no-store" }),
  ]);
  // /agents is the roster; without it there is nothing to show.
  if (agentsRes.status !== "fulfilled" || !agentsRes.value.ok) {
    const why = agentsRes.status === "fulfilled" ? `status ${agentsRes.value.status}` : "unreachable";
    throw new Error(`agent-server /agents ${why}`);
  }
  const body = (await agentsRes.value.json()) as { agents?: ServerAgent[] };
  const agents = Array.isArray(body.agents) ? body.agents : [];
  // /health only adds liveness and queue depth; losing it degrades, not fails.
  let health: Record<string, HealthAgent> = {};
  if (healthRes.status === "fulfilled" && healthRes.value.ok) {
    try {
      health = ((await healthRes.value.json()) as { agents?: Record<string, HealthAgent> }).agents || {};
    } catch {
      health = {};
    }
  }
  cache = { at: Date.now(), agents, health };
  return cache;
}

/** Test hook: drop the short-lived response cache. */
export function resetPackageStatusCache(): void {
  cache = null;
}

/** Merge /agents, /health and the registry into roster rows. Pure, so tests
 * cover it without a server. Server-reported agents come first in server
 * order; registry-only agents (declared but not yet registered with the
 * server) follow with state UNKNOWN. */
export function buildRoster(
  serverAgents: ServerAgent[],
  health: Record<string, HealthAgent>,
  registry: RegistryAgent[],
  allowed: (name: string) => boolean
): AgentRosterRow[] {
  const reg = new Map(registry.map((a) => [a.id, a]));
  const byName = new Map(serverAgents.map((a) => [a.name, a]));
  const names = [...byName.keys(), ...registry.map((a) => a.id).filter((id) => !byName.has(id))];

  return names
    .filter((name) => allowed(name))
    .map((name) => {
      const srv = byName.get(name);
      const r = reg.get(name);
      const h = health[name];

      // Shards: the server's rows (2.1 list, or the 1.5 dict normalised), with
      // the registry laid over them. A registry shard the server has not
      // reported is appended as UNKNOWN so an unspawned shard stays visible;
      // registry channels fill a row only when the server row has none.
      const shards = normalizeShards(srv?.shards);
      const serverShardCount = shards.length;
      for (const [sid, channels] of Object.entries(r?.shards || {})) {
        const existing = shards.find((x) => x.id === sid);
        if (existing) {
          if (existing.channels.length === 0) existing.channels = [...channels];
        } else {
          shards.push({ ...defaultShardRow(sid), channels: [...channels] });
        }
      }

      // Agent-level queue depth: the shard sum when the server reported shard
      // rows with depths (2.1 list), else /health's figure.
      const hasServerDepths = Array.isArray(srv?.shards) && serverShardCount > 0;
      const healthDepth = typeof h?.queue_depth === "number" ? h.queue_depth : 0;
      const queueDepth = hasServerDepths ? shards.reduce((n, x) => n + x.queue_depth, 0) : healthDepth;
      const allPaused = shards.length > 0 && shards.every((x) => x.paused);
      const row: AgentRosterRow = {
        name,
        state: srv?.state || h?.state || "UNKNOWN",
        host: PACKAGE_HOST,
        label: srv?.label || r?.label || name,
        messages_processed: 0,
        subprocess_alive: typeof h?.alive === "boolean" ? h.alive : undefined,
        queue_depths: {},
        total_pending: queueDepth,
        session_id: h?.session_id || undefined,
        compaction_count: 0,
        context_tokens: srv?.context_tokens ?? h?.context_tokens ?? 0,
        shards,
        reported: Boolean(srv),
        paused: allPaused ? shards[0].paused : null,
        role: r?.role ?? undefined,
        dashboard_chat: srv?.dashboard_chat ?? r?.dashboard_chat ?? true,
        model: srv?.model ?? r?.model ?? null,
      };
      return row;
    });
}

async function listAgents(allowed: (name: string) => boolean): Promise<AgentRosterRow[]> {
  const { agents, health } = await fetchServer();
  return buildRoster(agents, health, readRegistry(), allowed);
}

export const packageAdapter: AgentStatusAdapter = { listAgents };
