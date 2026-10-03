/**
 * Memory browser backend: GET /graph/* on the agent server
 * (package docs/graph-browse-api.md). Server-side only; imports lib/api and
 * nothing else. Client code uses lib/memoryView.ts, which takes types from
 * here with `import type` (erased at build).
 */

import { agentFetch } from "@/lib/api";

const FETCH_TIMEOUT_MS = 5000;

export const MEMORY_KINDS = ["fact", "episode", "pattern"] as const;
export const OBSERVATION_STATES = ["active", "archived", "superseded", "all"] as const;
export const ENTITY_STATES = ["active", "archived", "all"] as const;
export type MemoryKind = (typeof MEMORY_KINDS)[number];
export type ObservationState = (typeof OBSERVATION_STATES)[number];

export const MEMORY_DEFAULT_LIMIT = 25;
export const MEMORY_MAX_LIMIT = 100;
export const MEMORY_MAX_Q = 200;
export const MEMORY_MAX_FIELD = 80;
const CURSOR_RE = /^(b|o):\d+$/;

export interface GraphStatus {
  schema: number;
  entities: number;
  edges: number;
  observations: Record<MemoryKind, number>;
  archived: number;
  superseded: number;
  embed_model: string | null;
  last_consolidation: { started?: string; finished?: string; [key: string]: unknown } | null;
}

export interface EntityRef {
  id: number;
  name: string;
  kind?: string;
}

export interface Observation {
  id: number;
  kind: MemoryKind;
  subkind: string | null;
  content: string;
  entity: EntityRef | null;
  mentions: { id: number; name: string }[];
  importance: number | null;
  confidence: number | null;
  domain: string | null;
  agent: string | null;
  channel: string | null;
  tags: string[];
  reinforcement_count: number | null;
  source: string;
  created_at: string | null;
  updated_at: string | null;
  consolidated_at: string | null;
  archived_at: string | null;
  superseded_by: number | null;
}

export interface EntityRow {
  id: number;
  name: string;
  kind: string;
  summary: string | null;
  importance: number | null;
  last_seen_at: string | null;
  archived_at: string | null;
  observation_count: number;
  edge_count: number;
}

export interface Neighbor {
  id: number;
  name: string;
  kind: string;
  relation: string;
  weight: number;
  direction: "in" | "out";
}

export interface EntityDetail {
  entity: Omit<EntityRow, "observation_count" | "edge_count"> & {
    created_at: string | null;
    updated_at: string | null;
    aliases: string[];
  };
  neighbors: Neighbor[];
  counts: Record<MemoryKind, number>;
}

export interface ObservationPage { observations: Observation[]; next: string | null }
export interface EntityPage { entities: EntityRow[]; next: string | null }

export type MemoryResult<T> =
  | { ok: true; body: T }
  | { ok: false; status: number; error: string; body?: { error?: string; detail?: string } };

/** What a browse request may carry. All optional; see parseMemoryFilter. */
export interface MemoryFilter {
  q?: string;
  kind?: string;
  state?: string;
  entity?: number;
  domain?: string;
  agent?: string;
  limit?: number;
  cursor?: string;
}

export type FilterTarget = "observations" | "entities";

/**
 * Validate the dashboard-facing query and rebuild it for the server. Same
 * rules as the package: `kind` and `state` enums, integer `entity`, integer
 * `limit` 1..100, `cursor` `^(b|o):\d+$`, `q` at most 200 characters, `domain`
 * and `agent` at most 80. Unknown parameters are dropped. For `entities` the
 * kind is free text (an entity kind) and the state enum has no `superseded`.
 */
export function parseMemoryFilter(
  params: URLSearchParams,
  target: FilterTarget = "observations"
): { ok: true; query: string; filter: MemoryFilter } | { ok: false; error: string } {
  const out = new URLSearchParams();
  const filter: MemoryFilter = {};
  const get = (k: string) => {
    const v = params.get(k);
    return v === null || v === "" ? null : v;
  };
  const put = (k: keyof MemoryFilter, v: string | number) => {
    out.set(k, String(v));
    (filter as Record<string, string | number>)[k] = v;
  };

  const q = get("q");
  if (q !== null) {
    if (q.length > MEMORY_MAX_Q) return { ok: false, error: `q must be at most ${MEMORY_MAX_Q} characters` };
    put("q", q);
  }

  const kind = get("kind");
  if (kind !== null) {
    if (target === "observations") {
      if (!(MEMORY_KINDS as readonly string[]).includes(kind)) return { ok: false, error: "invalid kind" };
    } else if (kind.length > MEMORY_MAX_FIELD) {
      return { ok: false, error: `kind must be at most ${MEMORY_MAX_FIELD} characters` };
    }
    put("kind", kind);
  }

  const state = get("state");
  if (state !== null) {
    const allowed: readonly string[] = target === "observations" ? OBSERVATION_STATES : ENTITY_STATES;
    if (!allowed.includes(state)) return { ok: false, error: "invalid state" };
    put("state", state);
  }

  if (target === "observations") {
    const entity = get("entity");
    if (entity !== null) {
      if (!/^\d{1,12}$/.test(entity)) return { ok: false, error: "entity must be an integer" };
      put("entity", Number(entity));
    }
    for (const k of ["domain", "agent"] as const) {
      const v = get(k);
      if (v === null) continue;
      if (v.length > MEMORY_MAX_FIELD) return { ok: false, error: `${k} must be at most ${MEMORY_MAX_FIELD} characters` };
      put(k, v);
    }
  }

  const limit = get("limit");
  if (limit !== null) {
    if (!/^\d+$/.test(limit)) return { ok: false, error: "limit must be an integer" };
    const n = Number(limit);
    if (n < 1 || n > MEMORY_MAX_LIMIT) return { ok: false, error: `limit must be 1 to ${MEMORY_MAX_LIMIT}` };
    put("limit", n);
  }

  const cursor = get("cursor");
  if (cursor !== null) {
    if (!CURSOR_RE.test(cursor)) return { ok: false, error: "invalid cursor" };
    put("cursor", cursor);
  }

  return { ok: true, query: out.toString(), filter };
}

async function getJson<T>(path: string): Promise<MemoryResult<T>> {
  let res: Response;
  try {
    res = await agentFetch(path, { cache: "no-store", signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
  } catch {
    return { ok: false, status: 502, error: "agent-server unavailable" };
  }
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (res.ok && body !== null) return { ok: true, body: body as T };
  const errBody = body && typeof body === "object" ? (body as { error?: string; detail?: string }) : undefined;
  return { ok: false, status: res.status, error: errBody?.error ?? `HTTP ${res.status}`, body: errBody };
}

const withQuery = (path: string, query?: string) => (query ? `${path}?${query}` : path);

export const fetchGraphStatus = () => getJson<GraphStatus>("/graph/status");
/** `query` is the string from parseMemoryFilter. */
export const fetchObservations = (query?: string) => getJson<ObservationPage>(withQuery("/graph/observations", query));
export const fetchEntities = (query?: string) => getJson<EntityPage>(withQuery("/graph/entities", query));
export const fetchEntity = (id: number | string) => getJson<EntityDetail>(`/graph/entities/${encodeURIComponent(String(id))}`);
