/**
 * Pure helpers for the /memory page: the view state held in the
 * URL, the /api/memory/* URLs built from it, and display formatting. No
 * server imports; types come from lib/memoryBackend with `import type`.
 */
import type { MemoryKind, ObservationState } from "@/lib/memoryBackend";

export interface MemoryView {
  q: string;
  kind: MemoryKind | "";
  state: ObservationState;
  entity: number | null;
  domain: string;
}

export const EMPTY_VIEW: MemoryView = { q: "", kind: "", state: "active", entity: null, domain: "" };

export const STATE_LABELS: Record<ObservationState, string> = {
  active: "Active",
  archived: "Archived",
  superseded: "Superseded",
  all: "All",
};

const KINDS: readonly string[] = ["fact", "episode", "pattern"];
const STATES: readonly string[] = ["active", "archived", "superseded", "all"];

type Params = { get(name: string): string | null };

/** Read a view from page search params; anything invalid falls back to the default. */
export function parseView(params: Params): MemoryView {
  const kind = params.get("kind") ?? "";
  const state = params.get("state") ?? "active";
  const entity = params.get("entity") ?? "";
  return {
    q: (params.get("q") ?? "").slice(0, 200),
    kind: KINDS.includes(kind) ? (kind as MemoryKind) : "",
    state: STATES.includes(state) ? (state as ObservationState) : "active",
    entity: /^\d{1,12}$/.test(entity) ? Number(entity) : null,
    domain: (params.get("domain") ?? "").slice(0, 80),
  };
}

/** The shareable page URL for a view; defaults are omitted. */
export function viewHref(v: MemoryView): string {
  const p = new URLSearchParams();
  if (v.q) p.set("q", v.q);
  if (v.kind) p.set("kind", v.kind);
  if (v.state !== "active") p.set("state", v.state);
  if (v.entity !== null) p.set("entity", String(v.entity));
  if (v.domain) p.set("domain", v.domain);
  const s = p.toString();
  return s ? `/memory?${s}` : "/memory";
}

export const PAGE_SIZE = 25;

/**
 * /api/memory/observations URL. `scope` "all" is the main list (every
 * observation, the entity filter does not apply there); "entity" is the
 * panel's list (entity set, kind and state inherited, search not).
 */
export function observationsUrl(v: MemoryView, scope: "all" | "entity", cursor?: string | null): string {
  const p = new URLSearchParams();
  if (scope === "all" && v.q) p.set("q", v.q);
  if (v.kind) p.set("kind", v.kind);
  if (v.state !== "active") p.set("state", v.state);
  if (scope === "all" && v.domain) p.set("domain", v.domain);
  if (scope === "entity" && v.entity !== null) p.set("entity", String(v.entity));
  p.set("limit", String(PAGE_SIZE));
  if (cursor) p.set("cursor", cursor);
  return `/api/memory/observations?${p.toString()}`;
}

export function entitiesUrl(q: string, limit = 8): string {
  const p = new URLSearchParams();
  if (q) p.set("q", q);
  p.set("limit", String(limit));
  return `/api/memory/entities?${p.toString()}`;
}

/** "2026-10-03 12:29 UTC" from an ISO or "YYYY-MM-DD HH:MM:SS" time; "" when unusable. */
export function formatWhen(raw: string | null | undefined): string {
  if (!raw) return "";
  const t = Date.parse(raw.includes("T") ? raw : raw.replace(" ", "T") + "Z");
  if (Number.isNaN(t)) return "";
  return new Date(t).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

/** What to say when a request fails. */
export function errorMessage(status: number): string {
  if (status === 503) return "The memory graph is not set up yet. Run karakos migrate.";
  if (status === 403) return "Memory is not available to this account.";
  return "Memory is unavailable right now.";
}
