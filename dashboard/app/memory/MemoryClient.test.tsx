import { describe, expect, it } from "vitest";
import { readFileSync } from "fs";
import { join } from "path";
import { renderToStaticMarkup } from "react-dom/server";
import {
  EntityPanel,
  FilterBar,
  MemoryHeader,
  ObservationCard,
  ObservationList,
  type ListState,
} from "./MemoryClient";
import { EMPTY_VIEW, observationsUrl, parseView, viewHref } from "@/lib/memoryView";
import type { EntityDetail, GraphStatus, Observation } from "@/lib/memoryBackend";

const obs = (over: Partial<Observation>): Observation => ({
  id: 1, kind: "fact", subkind: null, content: "alpha prefers green tea", entity: { id: 4, name: "Alpha", kind: "person" },
  mentions: [], importance: 5, confidence: 0.8, domain: null, agent: "alpha", channel: null, tags: [], reinforcement_count: 1,
  source: "write", created_at: "2026-10-03T12:29:56Z", updated_at: null, consolidated_at: null, archived_at: null, superseded_by: null,
  ...over,
});
const list = (items: Observation[], over: Partial<ListState> = {}): ListState => ({ items, next: null, loading: false, error: null, ...over });

describe("ObservationList", () => {
  it("renders rows with kind, entity chip, importance, agent and time", () => {
    const html = renderToStaticMarkup(<ObservationList state={list([obs({}), obs({ id: 2, kind: "episode", entity: null })])} filtered={false} />);
    expect(html).toContain("alpha prefers green tea");
    expect(html).toContain(">Alpha<");
    expect(html).toContain("importance 5");
    expect(html).toContain("2026-10-03 12:29 UTC");
    expect(html).toContain(">episode<");
    expect(html).toContain("line-clamp-3");
  });

  it("shows archived and superseded badges", () => {
    const html = renderToStaticMarkup(
      <ObservationList state={list([obs({ id: 2, archived_at: "2026-10-01T00:00:00Z" }), obs({ id: 3, superseded_by: 9 })])} filtered={false} />
    );
    expect(html).toContain(">archived<");
    expect(html).toContain(">superseded<");
    expect(renderToStaticMarkup(<ObservationList state={list([obs({})])} filtered={false} />)).not.toContain(">archived<");
  });

  it("offers an expand control only for long content", () => {
    expect(renderToStaticMarkup(<ObservationCard o={obs({})} />)).not.toContain("Show more");
    expect(renderToStaticMarkup(<ObservationCard o={obs({ content: "x".repeat(400) })} />)).toContain("Show more");
  });

  it("says the graph is not set up on a 503", () => {
    const html = renderToStaticMarkup(<ObservationList state={list([], { error: 503 })} filtered={false} />);
    expect(html).toContain("The memory graph is not set up yet. Run karakos migrate.");
  });

  it("has distinct empty states", () => {
    expect(renderToStaticMarkup(<ObservationList state={list([])} filtered={false} />)).toContain("No observations yet.");
    expect(renderToStaticMarkup(<ObservationList state={list([])} filtered />)).toContain("No observations match.");
  });

  it("shows Load more only when there is a next cursor", () => {
    expect(renderToStaticMarkup(<ObservationList state={list([obs({})], { next: "b:1" })} filtered={false} />)).toContain("Load more");
    expect(renderToStaticMarkup(<ObservationList state={list([obs({})])} filtered={false} />)).not.toContain("Load more");
  });
});

describe("Load more", () => {
  it("requests the cursor from `next`", () => {
    const url = observationsUrl({ ...EMPTY_VIEW, kind: "fact" }, "all", "b:42");
    expect(url).toBe("/api/memory/observations?kind=fact&limit=25&cursor=b%3A42");
  });
});

describe("EntityPanel", () => {
  const detail: EntityDetail = {
    entity: { id: 4, name: "Alpha", kind: "person", summary: "The primary.", importance: 5, last_seen_at: null, archived_at: null, created_at: null, updated_at: null, aliases: ["al"] },
    neighbors: [
      { id: 5, name: "Beta", kind: "person", relation: "knows", weight: 1, direction: "out" },
      { id: 6, name: "Gamma", kind: "place", relation: "lives_in", weight: 1, direction: "in" },
    ],
    counts: { fact: 3, episode: 2, pattern: 1 },
  };
  it("renders summary, aliases, counts, neighbours with direction, and Clear", () => {
    const html = renderToStaticMarkup(<EntityPanel detail={detail} />);
    for (const s of ["Alpha", "The primary.", "Also: al", "3 facts · 2 episodes · 1 patterns", "Beta", "→ knows", "Gamma", "← lives_in", "Clear"]) {
      expect(html).toContain(s);
    }
  });
});

describe("header and filters", () => {
  const status: GraphStatus = {
    schema: 1, entities: 4, edges: 3, observations: { fact: 7, episode: 3, pattern: 2 }, archived: 1, superseded: 1,
    embed_model: "bge-small", last_consolidation: { finished: "2026-10-03T03:00:09Z" },
  };
  it("shows counts, model and last consolidation", () => {
    const html = renderToStaticMarkup(<MemoryHeader status={status} />);
    for (const s of ["Facts", ">7<", "Entities", "Edges", "Archived", "bge-small", "2026-10-03 03:00 UTC"]) expect(html).toContain(s);
    expect(renderToStaticMarkup(<MemoryHeader status={{ ...status, last_consolidation: null }} />)).toContain("never");
  });
  it("labels the search and lists the four states", () => {
    const html = renderToStaticMarkup(<FilterBar view={EMPTY_VIEW} counts={status.observations} />);
    expect(html).toContain("Keyword search");
    for (const s of ["Active", "Archived", "Superseded", "All"]) expect(html).toContain(`>${s}<`);
    expect(html).toContain("fact 7");
  });
});

describe("view state in the URL", () => {
  it("round-trips and ignores junk", () => {
    const v = parseView(new URLSearchParams("q=tea&kind=fact&state=archived&entity=7&domain=home"));
    expect(viewHref(v)).toBe("/memory?q=tea&kind=fact&state=archived&entity=7&domain=home");
    expect(parseView(new URLSearchParams("kind=zzz&state=zzz&entity=abc"))).toEqual(EMPTY_VIEW);
    expect(viewHref(EMPTY_VIEW)).toBe("/memory");
  });
});

describe("read-only", () => {
  it("issues only GETs and has no form", () => {
    const src = readFileSync(join(__dirname, "MemoryClient.tsx"), "utf-8");
    expect(src.match(/\bfetch\(/g)).toHaveLength(1);
    expect(src).not.toMatch(/method\s*:/i);
    expect(src).not.toMatch(/<form\b|onSubmit|XMLHttpRequest|sendBeacon/);
  });
});
