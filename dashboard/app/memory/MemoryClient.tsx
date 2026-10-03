"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { alpha, fieldStyle, SectionLabel, Tag, WARN, INFO, OK, REVIEW } from "@/app/components/lamplight-ui";
import type {
  EntityDetail,
  EntityRow,
  GraphStatus,
  MemoryKind,
  Neighbor,
  Observation,
  ObservationPage,
  ObservationState,
} from "@/lib/memoryBackend";
import {
  entitiesUrl,
  errorMessage,
  formatWhen,
  observationsUrl,
  parseView,
  STATE_LABELS,
  viewHref,
  type MemoryView,
} from "@/lib/memoryView";

/**
 * /memory: a read-only master/detail browser over the graph. The
 * view lives in the URL (?q=&kind=&state=&entity=&domain=). The page issues
 * only `GET /api/memory/*`; there is no form and nothing that writes.
 */

const KIND_COLOR: Record<MemoryKind, string> = { fact: INFO, episode: OK, pattern: REVIEW };
const KINDS: MemoryKind[] = ["fact", "episode", "pattern"];

// ---------- presentational pieces (static-markup tested) ----------

export function MemoryHeader({ status, error }: { status: GraphStatus | null; error?: number | null }) {
  if (error) return null;
  const n = status?.observations;
  const done = status?.last_consolidation?.finished ?? status?.last_consolidation?.started;
  const cells: [string, string | number][] = [
    ["Facts", n?.fact ?? "–"],
    ["Episodes", n?.episode ?? "–"],
    ["Patterns", n?.pattern ?? "–"],
    ["Entities", status?.entities ?? "–"],
    ["Edges", status?.edges ?? "–"],
    ["Archived", status?.archived ?? "–"],
  ];
  return (
    <section aria-label="Graph summary" className="mb-4">
      <dl className="flex flex-wrap gap-x-6 gap-y-2">
        {cells.map(([label, value]) => (
          <div key={label}>
            <dt className="text-[10px] uppercase" style={{ color: "var(--text-muted)", letterSpacing: "0.11em" }}>{label}</dt>
            <dd className="text-lg font-semibold" style={{ color: "var(--text-primary)" }}>{value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs mt-2" style={{ color: "var(--text-muted)" }}>
        Embedding model: {status?.embed_model ?? "unknown"} · Last consolidation:{" "}
        {status ? (formatWhen(done) || "never") : "…"}
      </p>
    </section>
  );
}

export function ObservationCard({
  o,
  onEntity,
  expandedInitially = false,
}: {
  o: Observation;
  onEntity?: (id: number) => void;
  expandedInitially?: boolean;
}) {
  const [open, setOpen] = useState(expandedInitially);
  const long = o.content.length > 220 || o.content.split("\n").length > 3;
  return (
    <li
      className="rounded-lg border p-3"
      style={{ backgroundColor: "var(--bg-surface)", borderColor: alpha("var(--ink)", 13) }}
    >
      <div className="flex flex-wrap items-center gap-2 mb-1.5">
        <Tag label={o.subkind ? `${o.kind} · ${o.subkind}` : o.kind} color={KIND_COLOR[o.kind] ?? INFO} />
        {o.archived_at && <Tag label="archived" color={WARN} />}
        {o.superseded_by !== null && <Tag label="superseded" color={REVIEW} />}
        {o.entity && (
          <button
            type="button"
            className="text-xs rounded-full px-2 py-0.5"
            style={{ border: `1px solid ${alpha("var(--accent)", 45)}`, color: "var(--accent)" }}
            onClick={() => onEntity?.(o.entity!.id)}
          >
            {o.entity.name}
          </button>
        )}
      </div>
      <p
        className={`text-sm whitespace-pre-wrap break-words ${open ? "" : "line-clamp-3"}`}
        style={{ color: "var(--text-primary)" }}
      >
        {o.content}
      </p>
      {long && (
        <button
          type="button"
          className="text-xs mt-1"
          style={{ color: "var(--text-muted)" }}
          aria-expanded={open}
          onClick={() => setOpen(!open)}
        >
          {open ? "Show less" : "Show more"}
        </button>
      )}
      <div className="flex flex-wrap gap-x-3 gap-y-1 mt-2 text-xs" style={{ color: "var(--text-muted)" }}>
        <span>importance {o.importance ?? "–"}</span>
        {o.agent && <span>{o.agent}</span>}
        {o.created_at && <span>{formatWhen(o.created_at)}</span>}
      </div>
    </li>
  );
}

export interface ListState {
  items: Observation[];
  next: string | null;
  loading: boolean;
  error: number | null;
}

export function ObservationList({
  state,
  filtered,
  onEntity,
  onMore,
  emptyLabel = "No observations yet.",
}: {
  state: ListState;
  /** True when a search or filter is active (picks the "no match" message). */
  filtered: boolean;
  onEntity?: (id: number) => void;
  onMore?: () => void;
  emptyLabel?: string;
}) {
  if (state.error) {
    return <p role="alert" className="text-sm" style={{ color: state.error === 503 ? "var(--text-secondary)" : "var(--err)" }}>{errorMessage(state.error)}</p>;
  }
  if (!state.loading && state.items.length === 0) {
    return (
      <p className="text-sm" style={{ color: "var(--text-muted)" }}>
        {filtered ? "No observations match." : emptyLabel}
      </p>
    );
  }
  return (
    <div>
      <ul className="grid gap-2" aria-label="Observations">
        {state.items.map((o) => (
          <ObservationCard key={o.id} o={o} onEntity={onEntity} />
        ))}
      </ul>
      {state.loading && <p className="text-sm mt-2" style={{ color: "var(--text-muted)" }}>Loading…</p>}
      {state.next && !state.loading && (
        <button
          type="button"
          className="mt-3 text-sm rounded-lg px-4"
          style={{ minHeight: 44, border: `1px solid ${alpha("var(--ink)", 20)}`, color: "var(--text-primary)" }}
          onClick={onMore}
        >
          Load more
        </button>
      )}
    </div>
  );
}

export function NeighborChip({ n, onPick }: { n: Neighbor; onPick?: (id: number) => void }) {
  return (
    <button
      type="button"
      className="text-xs rounded-full px-2.5 py-1"
      style={{ border: `1px solid ${alpha("var(--ink)", 20)}`, color: "var(--text-primary)" }}
      onClick={() => onPick?.(n.id)}
    >
      {n.name}
      <span style={{ color: "var(--text-muted)" }}>
        {" "}
        {n.direction === "out" ? "→" : "←"} {n.relation}
      </span>
    </button>
  );
}

export function EntityPanel({
  detail,
  onPick,
  onClear,
  children,
}: {
  detail: EntityDetail;
  onPick?: (id: number) => void;
  onClear?: () => void;
  children?: React.ReactNode;
}) {
  const e = detail.entity;
  return (
    <section
      aria-label="Entity"
      className="rounded-lg border p-3"
      style={{ backgroundColor: "var(--bg-surface)", borderColor: alpha("var(--ink)", 13) }}
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold" style={{ color: "var(--text-primary)" }}>{e.name}</h2>
          <div className="mt-1 flex gap-2 items-center">
            <Tag label={e.kind} color={INFO} />
            {e.archived_at && <Tag label="archived" color={WARN} />}
          </div>
        </div>
        <button
          type="button"
          className="text-sm rounded-lg px-3"
          style={{ minHeight: 44, border: `1px solid ${alpha("var(--ink)", 20)}`, color: "var(--text-primary)" }}
          onClick={onClear}
        >
          Clear
        </button>
      </div>
      {e.summary && <p className="text-sm mt-2" style={{ color: "var(--text-secondary)" }}>{e.summary}</p>}
      {e.aliases.length > 0 && (
        <p className="text-xs mt-2" style={{ color: "var(--text-muted)" }}>Also: {e.aliases.join(", ")}</p>
      )}
      <p className="text-xs mt-2" style={{ color: "var(--text-muted)" }}>
        {detail.counts.fact} facts · {detail.counts.episode} episodes · {detail.counts.pattern} patterns
      </p>
      {detail.neighbors.length > 0 && (
        <div className="mt-3">
          <SectionLabel>neighbours</SectionLabel>
          <div className="flex flex-wrap gap-1.5 mt-1.5">
            {detail.neighbors.map((n) => (
              <NeighborChip key={`${n.direction}-${n.id}-${n.relation}`} n={n} onPick={onPick} />
            ))}
          </div>
        </div>
      )}
      {children && <div className="mt-4">{children}</div>}
    </section>
  );
}

export function FilterBar({
  view,
  counts,
  onChange,
}: {
  view: MemoryView;
  counts?: Record<MemoryKind, number>;
  onChange?: (patch: Partial<MemoryView>) => void;
}) {
  return (
    <div className="grid gap-2 mb-3" role="search">
      <label className="grid gap-1 text-xs" style={{ color: "var(--text-muted)" }}>
        Keyword search
        <input
          type="search"
          defaultValue={view.q}
          maxLength={200}
          placeholder="Search observations"
          style={fieldStyle}
          onKeyDown={(e) => {
            if (e.key === "Enter") onChange?.({ q: (e.target as HTMLInputElement).value.trim() });
          }}
          onBlur={(e) => {
            if (e.target.value.trim() !== view.q) onChange?.({ q: e.target.value.trim() });
          }}
        />
      </label>
      <div className="flex flex-wrap items-center gap-2">
        {KINDS.map((k) => {
          const on = view.kind === k;
          return (
            <button
              key={k}
              type="button"
              aria-pressed={on}
              className="text-xs rounded-full px-3"
              style={{
                minHeight: 36,
                border: `1px solid ${alpha(KIND_COLOR[k], on ? 90 : 45)}`,
                background: on ? alpha(KIND_COLOR[k], 22) : "transparent",
                color: "var(--text-primary)",
              }}
              onClick={() => onChange?.({ kind: on ? "" : k })}
            >
              {k}
              {counts ? ` ${counts[k]}` : ""}
            </button>
          );
        })}
        <label className="ml-auto flex items-center gap-2 text-xs" style={{ color: "var(--text-muted)" }}>
          State
          <select
            value={view.state}
            style={{ ...fieldStyle, width: "auto" }}
            onChange={(e) => onChange?.({ state: e.target.value as ObservationState })}
          >
            {(Object.keys(STATE_LABELS) as ObservationState[]).map((s) => (
              <option key={s} value={s}>{STATE_LABELS[s]}</option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}

// ---------- data ----------

type Fetched<T> = { ok: true; body: T } | { ok: false; status: number };

/** The only network call on this page: a GET of a same-origin /api/memory route. */
async function getJson<T>(url: string): Promise<Fetched<T>> {
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) return { ok: false, status: res.status };
    return { ok: true, body: (await res.json()) as T };
  } catch {
    return { ok: false, status: 0 };
  }
}

const IDLE: ListState = { items: [], next: null, loading: true, error: null };

function useObservations(url: string | null) {
  const [state, setState] = useState<ListState>(IDLE);
  const gen = useRef(0);
  useEffect(() => {
    if (!url) return;
    const g = ++gen.current;
    setState(IDLE);
    getJson<ObservationPage>(url).then((r) => {
      if (g !== gen.current) return;
      setState(r.ok ? { items: r.body.observations, next: r.body.next, loading: false, error: null } : { items: [], next: null, loading: false, error: r.status || 502 });
    });
  }, [url]);
  const more = useCallback(
    (nextUrl: string) => {
      const g = gen.current;
      setState((s) => ({ ...s, loading: true }));
      getJson<ObservationPage>(nextUrl).then((r) => {
        if (g !== gen.current) return;
        setState((s) =>
          r.ok
            ? { items: [...s.items, ...r.body.observations], next: r.body.next, loading: false, error: null }
            : { ...s, loading: false, error: r.status || 502 }
        );
      });
    },
    []
  );
  return [state, more] as const;
}

// ---------- page ----------

export default function MemoryClient({ initial }: { initial: MemoryView }) {
  const [view, setView] = useState<MemoryView>(initial);

  // Keep the URL in step with the view, and follow back/forward.
  const change = useCallback((patch: Partial<MemoryView>) => {
    setView((v) => {
      const next = { ...v, ...patch };
      window.history.pushState(null, "", viewHref(next));
      return next;
    });
  }, []);
  useEffect(() => {
    const onPop = () => setView(parseView(new URLSearchParams(window.location.search)));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const [status, setStatus] = useState<GraphStatus | null>(null);
  const [statusError, setStatusError] = useState<number | null>(null);
  useEffect(() => {
    getJson<GraphStatus>("/api/memory/status").then((r) => (r.ok ? setStatus(r.body) : setStatusError(r.status || 502)));
  }, []);

  const mainUrl = observationsUrl(view, "all");
  const [main, moreMain] = useObservations(mainUrl);

  const entityUrl = view.entity !== null ? observationsUrl(view, "entity") : null;
  const [entityObs, moreEntity] = useObservations(entityUrl);
  const [detail, setDetail] = useState<{ id: number; r: Fetched<EntityDetail> } | null>(null);
  useEffect(() => {
    if (view.entity === null) return;
    const id = view.entity;
    getJson<EntityDetail>(`/api/memory/entities/${id}`).then((r) => setDetail({ id, r }));
  }, [view.entity]);
  const shown = view.entity !== null && detail?.id === view.entity ? detail.r : null;

  const pickEntity = (id: number) => change({ entity: id });
  const filtered = !!(view.q || view.kind || view.domain || view.state !== "active");

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-4" style={{ color: "var(--text-primary)" }}>Memory</h1>
      <MemoryHeader status={status} error={statusError === 503 ? statusError : null} />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div>
          <FilterBar view={view} counts={status?.observations} onChange={change} />
          <ObservationList
            state={main}
            filtered={filtered}
            onEntity={pickEntity}
            onMore={() => main.next && moreMain(observationsUrl(view, "all", main.next))}
          />
        </div>
        <aside aria-label="Entity detail">
          <EntityPicker onPick={pickEntity} />
          {view.entity !== null && shown && !shown.ok && (
            <p role="alert" className="text-sm mt-3" style={{ color: "var(--err)" }}>
              {shown.status === 404 ? "No such entity." : errorMessage(shown.status)}
            </p>
          )}
          {view.entity !== null && shown?.ok && (
            <div className="mt-3">
              <EntityPanel detail={shown.body} onPick={pickEntity} onClear={() => change({ entity: null })}>
                <SectionLabel style={{ marginBottom: 8 }}>observations</SectionLabel>
                <ObservationList
                  state={entityObs}
                  filtered={!!(view.kind || view.state !== "active")}
                  onEntity={pickEntity}
                  onMore={() => entityObs.next && moreEntity(observationsUrl(view, "entity", entityObs.next))}
                  emptyLabel="No observations for this entity."
                />
              </EntityPanel>
            </div>
          )}
          {view.entity === null && (
            <p className="text-sm mt-3" style={{ color: "var(--text-muted)" }}>
              Pick an entity chip or search for one to see its neighbours and observations.
            </p>
          )}
        </aside>
      </div>
    </div>
  );
}

export function EntityPicker({ onPick }: { onPick: (id: number) => void }) {
  const [text, setText] = useState("");
  const [rows, setRows] = useState<EntityRow[]>([]);
  useEffect(() => {
    const q = text.trim();
    if (!q) {
      setRows([]);
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      getJson<{ entities: EntityRow[] }>(entitiesUrl(q)).then((r) => live && setRows(r.ok ? r.body.entities : []));
    }, 250);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [text]);
  return (
    <div>
      <label className="grid gap-1 text-xs" style={{ color: "var(--text-muted)" }}>
        Find an entity
        <input
          type="search"
          value={text}
          maxLength={200}
          placeholder="Entity name or alias"
          style={fieldStyle}
          onChange={(e) => setText(e.target.value)}
        />
      </label>
      {rows.length > 0 && (
        <ul className="flex flex-wrap gap-1.5 mt-2" aria-label="Matching entities">
          {rows.map((r) => (
            <li key={r.id}>
              <button
                type="button"
                className="text-xs rounded-full px-2.5 py-1"
                style={{ border: `1px solid ${alpha("var(--ink)", 20)}`, color: "var(--text-primary)" }}
                onClick={() => {
                  setText("");
                  onPick(r.id);
                }}
              >
                {r.name} <span style={{ color: "var(--text-muted)" }}>{r.kind}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
