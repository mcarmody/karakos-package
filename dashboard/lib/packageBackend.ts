/**
 * Package backend helpers: GET /usage and GET /hive/calls from
 * the agent server. A non-2xx answer or a network error
 * comes back as `{ok: false, status}` rather than a throw, so a route can turn
 * it into a 502.
 */

import { agentFetch, AGENT_NAME_RE } from "@/lib/api";
import { HIVE_STATUSES, hiveQuery, type HiveQuery, type HiveStatus } from "@/lib/fleetView";

export { HIVE_STATUSES, hiveQuery, type HiveQuery, type HiveStatus };

const FETCH_TIMEOUT_MS = 5000;

export const HIVE_DEFAULT_LIMIT = 100;
export const HIVE_MAX_LIMIT = 500;

export interface UsageWindow {
  status: string | null;
  resets_at: number | null;
  utilization_pct: number | null;
  percent_of_window_used?: number | null;
  updated_at: string | null;
}

export interface UsageAgent {
  status: string | null;
  rate_limit_type: string | null;
  resets_at: number | null;
  percent_of_window_used: number | null;
  summary?: string;
  updated_at: string | null;
  [key: string]: unknown;
}

export interface UsageBudget {
  used: number;
  budget: number;
  paused_since: number | null;
  until: number | null;
}

/** Body of GET /usage: `agents` as before, plus the 2.7 sections. */
export interface UsageBody {
  agents?: Record<string, UsageAgent>;
  windows?: Record<string, UsageWindow>;
  breaker?: { paused: boolean; until: number | null; types: string[] };
  budgets?: Record<string, UsageBudget>;
  governor?: { weekly_pct: number | null; enabled: boolean; policy_broken: boolean };
}

export interface HiveCall {
  call_id: string;
  from?: string;
  to?: string;
  from_agent: string;
  to_agent: string;
  depth: number;
  status: HiveStatus | string;
  created_at: string;
  started_at: string | null;
  answered_at: string | null;
  duration_ms: number | null;
  question: string | null;
  answer: string | null;
  error: string | null;
}

export type BackendResult<T> = { ok: true; data: T } | { ok: false; status: number };

async function getJson<T>(path: string): Promise<BackendResult<T>> {
  try {
    const res = await agentFetch(path, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS), cache: "no-store" });
    // Any non-2xx is the agent server being unavailable as far as the routes go.
    if (!res.ok) return { ok: false, status: 502 };
    return { ok: true, data: (await res.json()) as T };
  } catch {
    return { ok: false, status: 502 };
  }
}

export function fetchUsage(): Promise<BackendResult<UsageBody>> {
  return getJson<UsageBody>("/usage");
}

/** `query` is the already-validated string from `hiveQuery(parseHiveFilter(...))`. */
export function fetchHiveCalls(query: HiveQuery): Promise<BackendResult<{ calls: HiveCall[] }>> {
  return getJson<{ calls: HiveCall[] }>(`/hive/calls${hiveQuery(query) ? `?${hiveQuery(query)}` : ""}`);
}

/** Validate the dashboard-facing query. Unknown parameters are dropped, so
 * nothing reaches the server unvalidated. */
export function parseHiveFilter(params: URLSearchParams): { ok: true; query: HiveQuery } | { ok: false; error: string } {
  const query: HiveQuery = { limit: HIVE_DEFAULT_LIMIT };

  const limit = params.get("limit");
  if (limit !== null) {
    if (!/^\d+$/.test(limit)) return { ok: false, error: "limit must be an integer" };
    const n = Number(limit);
    if (n < 1 || n > HIVE_MAX_LIMIT) return { ok: false, error: `limit must be 1 to ${HIVE_MAX_LIMIT}` };
    query.limit = n;
  }

  const since = params.get("since");
  if (since !== null && since !== "") {
    // ISO-8601 only: a bare date or date-time with optional zone. Date.parse
    // alone also accepts "March 3" and the like, which the server would not.
    if (!/^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/.test(since) || Number.isNaN(Date.parse(since))) {
      return { ok: false, error: "since must be an ISO-8601 time" };
    }
    query.since = since;
  }

  const shard = params.get("shard");
  if (shard !== null && shard !== "") {
    if (!AGENT_NAME_RE.test(shard)) return { ok: false, error: "invalid shard" };
    query.shard = shard;
  }

  const status = params.get("status");
  if (status !== null && status !== "") {
    if (!(HIVE_STATUSES as readonly string[]).includes(status)) return { ok: false, error: "invalid status" };
    query.status = status as HiveStatus;
  }

  return { ok: true, query };
}
