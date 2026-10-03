"use client";

/**
 * The chat surface, shared by the /chat page and any embedded panel.
 *
 * `variant="page"` is the full-page layout; everything the panel does
 * differently is behind `variant === "panel"` or the `agent` prop.
 */

import { useState, useEffect, useRef, useCallback, FormEvent, KeyboardEvent, ChangeEvent, ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { usePoll } from "@/lib/hooks";
import ConversationMetricsBadge from "@/app/components/ConversationMetricsBadge";
import { buildSessionList, defaultSessionName, friendlyName, type SessionOption } from "@/lib/sessionRoster";
import { isPassResponse, terminalStatusMessage, type ChatAttachment } from "@/lib/chatMessage";
import { isAtBottom, shouldFollow, onScrollState } from "@/lib/chatScroll";
import SessionPicker from "@/app/components/SessionPicker";

interface AgentInfo {
  name: string;
  state: string;
  host?: string;
  subprocess_alive?: boolean;
  label?: string;
  role?: string;
}

interface AgentList {
  agents: AgentInfo[];
}

interface AgentStatus {
  busy: boolean;
  state?: "idle" | "unknown" | "replying" | "turn-elsewhere" | "batch";
  detail?: string;
}

// Status-dot palette: what the agent is doing → color + label.
// idle renders dim and still; everything else breathes.
const STATUS_DOT: Record<
  Exclude<AgentStatus["state"], undefined>,
  { color: string; label: (detail?: string) => string; pulse: boolean }
> = {
  idle: { color: "var(--ink)", label: () => "idle", pulse: false },
  // Nothing is reporting busy/idle for this agent (see lib/busyState.ts
  // classifyBusyKnown) — say so rather than show a confident "idle".
  unknown: { color: "var(--ink)", label: () => "status unknown", pulse: false },
  replying: { color: "var(--ok)", label: () => "replying…", pulse: true },
  "turn-elsewhere": {
    color: "var(--warn)",
    label: (d) => (d ? `busy in #${d}…` : "busy elsewhere…"),
    pulse: true,
  },
  batch: {
    color: "var(--info)",
    label: (d) => (d ? `working: ${d}…` : "working…"),
    pulse: true,
  },
};

// Typed mid-turn event from the supervisor's dashboard_events_pump,
// relayed by /api/chat/stream as `{event: {...}}` SSE payloads.
// Thinking renders at 34%, interstitials at 66%,
// and neither ever carries conclusions — the final answer is the chunk
// stream, same as before.
interface TurnEvent {
  kind: "thinking" | "interstitial" | "tool";
  content: string;
  seq: number;
  /** Client-side arrival time (ms) — drives the frozen "thinking · Ns" label. */
  arrivedAt?: number;
}

interface ChatMessage {
  role: "user" | "assistant" | "sys";
  content: string;
  ts: string;
  messageId?: string;
  events?: TurnEvent[];
  /**
   * role: "assistant" only — when the agent's turn ENDED, if it has.
   * Set from message_queue.posted_at (see /api/chat/history's turnEndedAt),
   * or stamped locally the moment the stream reports `done`. Its presence
   * is what draws the TurnBoundary rule below the turn, and it is
   * deliberately independent of `content`: a turn that produced no text at
   * all still ends, and that silence is the case the rule exists for.
   */
  turnEndedAt?: string;
  /** role: "assistant" only — non-complete terminal status banner text (crashed / skipped / ...). */
  terminalNote?: string;
  /** Files sent with a user message (discordParity surfaces). */
  attachments?: ChatAttachment[];
  /** role: "sys" only — the command and its args, for the slip's echo line. */
  cmd?: string;
  args?: string;
  /** role: "sys" only — the request failed; the slip renders in --warn. */
  sysError?: boolean;
}

interface PendingAttachment {
  id: string;
  name: string;
  size: number;
  /** discordParity only: upload state + the record /api/chat receives. */
  status?: "uploading" | "done" | "error";
  att?: ChatAttachment;
  previewUrl?: string;
}

// A reply that has been "working" this long is treated as dead, not live —
// keeps a stale processed=1 row from pulsing forever after a reload.
const LIVE_TURN_MAX_MS = 10 * 60 * 1000;

function isLiveTurn(m: ChatMessage): boolean {
  if (m.role !== "assistant" || !m.messageId || m.turnEndedAt) return false;
  const t = parseServerTs(m.ts).getTime();
  return !Number.isNaN(t) && Date.now() - t < LIVE_TURN_MAX_MS;
}

const URL_RE = /(https?:\/\/[^\s<>"']+)/g;

/** Make bare URLs in plain user text clickable. */
function linkify(text: string): ReactNode[] {
  return text.split(URL_RE).map((part, i) => {
    if (i % 2 === 0) return part;
    const m = /^(.*?)([.,;:!?)\]]*)$/.exec(part);
    const href = m ? m[1] : part;
    const tail = m ? m[2] : "";
    return (
      <span key={i}>
        <a href={href} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)", textDecoration: "underline" }}>
          {href}
        </a>
        {tail}
      </span>
    );
  });
}

type StatusPill = null | "saving" | "reviewing";

const MAX_TEXTAREA_ROWS = 8;
const IDB_DB_NAME = "karakos-chat";
const IDB_STORE = "history";
const IDB_VERSION = 1;

// Interstitial copy for the two states the status-pill heuristic can reach.
//
const INTERSTITIAL: Record<Exclude<StatusPill, null>, { label: string; body: string }> = {
  saving: { label: "saving", body: "writing this turn to memory" },
  reviewing: { label: "reviewing", body: "awaiting reviewer agent" },
};

// List stagger: 60–80ms/row, capped so a long history doesn't crawl in.
const STAGGER_MS = 70;
const STAGGER_CAP_ROWS = 6;

export interface ChatSurfaceProps {
  /**
   * Pin the surface to one agent. When set, the agent picker is not
   * rendered and the agent never changes — the caller owns that choice
   * When omitted,
   * behaviour is exactly what /chat does today: the picker, and a default
   * of the first chattable agent the roster actually has.
   */
  agent?: string;
  /**
   * "page" is the full /chat route, unchanged. "panel" is the embedded
   * side-panel: denser spacing, smaller type, and none of the page-level
   * chrome that only makes sense when chat owns the whole viewport — the
   * notch spacer, the TabShelf composer clearance, and the context rail
   * (which is itself a second column, and a column inside a column is not
   * a thing a 400px rail has room for).
   */
  variant?: "page" | "panel";
  /**
   * Chat-app feature/UX parity:
   * attachments end to end (pick / paste / drag-drop, thumbnails), sending
   * while the agent is replying (routed through the busy-confirm modal),
   * a typing-style "X is working…" indicator, sender-grouped messages with
   * hover/tap timestamps, scroll-pinning with a jump-to-latest pill, and
   * agent-name labels. Off by default.
   */
  discordParity?: boolean;
}

// Every number that differs between the full-page chat and the embedded
// panel lives here, so "does /chat still look identical" is answerable by
// reading one object instead of auditing a ternary at every style site.
// The `page` column is the literal set of values app/chat/page.tsx carried
// before this component was extracted from it — do not tune it unless you
// mean to change /chat.
const DENSITY = {
  page: {
    headerPad: "0 2px 12px",
    titleSize: 21,
    toolbarGap: 14,
    listGap: 20,
    listPad: "10px 2px 0",
    userSize: 15,
    answerSize: 14.5,
    answerPad: "15px 17px",
    caretHeight: 17,
    interstitialSize: 14,
    interstitialPad: "12px 16px",
    composerPadTop: 12,
    composerGutter: 2,
    composerMinHeight: 54,
    composerPad: "0 8px 0 19px",
  },
  panel: {
    headerPad: "0 0 8px",
    titleSize: 15,
    toolbarGap: 10,
    listGap: 13,
    listPad: "8px 0 0",
    userSize: 13.5,
    answerSize: 13.5,
    answerPad: "12px 13px",
    caretHeight: 15,
    interstitialSize: 13,
    interstitialPad: "10px 13px",
    composerPadTop: 10,
    composerGutter: 0,
    composerMinHeight: 46,
    composerPad: "0 6px 0 14px",
  },
} as const;

function density(dense: boolean) {
  return dense ? DENSITY.panel : DENSITY.page;
}

function staggerDelay(index: number): number {
  return Math.min(index, STAGGER_CAP_ROWS - 1) * STAGGER_MS;
}

function formatClock(d: Date): string {
  let h = d.getHours();
  const m = d.getMinutes();
  const ampm = h >= 12 ? "p" : "a";
  h = h % 12;
  if (h === 0) h = 12;
  return `${h}:${String(m).padStart(2, "0")}${ampm}`;
}

// Timestamps reach this page in two shapes and only one of them is safe to
// hand straight to `new Date()`:
//
//   "2026-08-26T17:38:46.123Z"  — locally minted here, real ISO, has a zone
//   "2026-08-26 17:38:46"       — straight out of SQLite `datetime('now')`,
//                                 which is UTC but carries NO zone marker
//
// V8 parses that second shape as LOCAL time, so a server timestamp rendered
// with a bare `new Date()` reads hours off (7 in America/Los_Angeles —
// a 10:38a turn labelled 5:38p). Normalize the bare shape to an explicit
// UTC instant; anything else is already unambiguous and passes through.
const SQLITE_UTC = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})$/;

function parseServerTs(ts: string): Date {
  const m = SQLITE_UTC.exec(ts);
  return new Date(m ? `${m[1]}T${m[2]}Z` : ts);
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

// Speculative <thinking>…</thinking> extraction — the agent-server plumbing
// in this repo only ever streams flat text today, so this is a no-op for
// real traffic until (if) the stream starts tagging a thinking segment.
// Building the surface now means nothing else needs to change when it does.
function splitThinking(content: string): { thinking: string | null; body: string; open: boolean } {
  const m = /^\s*<think(?:ing)?>([\s\S]*?)(<\/think(?:ing)?>)?/i.exec(content);
  if (!m || m[1] === undefined) return { thinking: null, body: content, open: false };
  const closed = !!m[2];
  return { thinking: m[1], body: content.slice(m[0].length), open: !closed };
}

// Open (or create) IndexedDB for chat history.
function openIdb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_DB_NAME, IDB_VERSION);
    req.onupgradeneeded = (e) => {
      const db = (e.target as IDBOpenDBRequest).result;
      if (!db.objectStoreNames.contains(IDB_STORE)) {
        db.createObjectStore(IDB_STORE);
      }
    };
    req.onsuccess = (e) => resolve((e.target as IDBOpenDBRequest).result);
    req.onerror = (e) => reject((e.target as IDBOpenDBRequest).error);
  });
}

async function idbGet(db: IDBDatabase, key: string): Promise<ChatMessage[] | null> {
  return new Promise((resolve) => {
    const tx = db.transaction(IDB_STORE, "readonly");
    const req = tx.objectStore(IDB_STORE).get(key);
    req.onsuccess = () => resolve(req.result ?? null);
    req.onerror = () => resolve(null);
  });
}

async function idbPut(db: IDBDatabase, key: string, value: ChatMessage[]): Promise<void> {
  return new Promise((resolve) => {
    const tx = db.transaction(IDB_STORE, "readwrite");
    tx.objectStore(IDB_STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onerror = () => resolve(); // best-effort
  });
}

// ---- Speech-hierarchy materials -----------------

function UserLine({
  text,
  delay,
  dense,
  parity,
  header,
  stamp,
  attachments,
  grouped,
}: {
  text: string;
  delay: number;
  dense: boolean;
  parity?: boolean;
  header?: string;
  stamp?: string;
  attachments?: ChatAttachment[];
  grouped?: boolean;
}) {
  const images = (attachments ?? []).filter((a) => a.content_type.startsWith("image/"));
  const files = (attachments ?? []).filter((a) => !a.content_type.startsWith("image/"));
  return (
    <div
      className={parity ? "anim-lift chat-msg" : "anim-lift"}
      tabIndex={parity ? 0 : undefined}
      style={{
        animationDelay: `${delay}ms`,
        alignSelf: "flex-end",
        maxWidth: "82%",
        textAlign: "right",
        paddingRight: 8,
        ...(grouped ? { marginTop: -7 } : {}),
      }}
    >
      {header && <div style={{ fontSize: 11.5, opacity: 0.5, marginBottom: 3, color: "var(--ink)" }}>{header}</div>}
      {images.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", justifyContent: "flex-end", gap: 6, marginBottom: 6 }}>
          {images.map((a, i) => (
            <a key={i} href={a.url} target="_blank" rel="noopener noreferrer" title={a.filename}>
              <img
                src={a.url}
                alt={a.filename}
                style={{ display: "block", maxWidth: 200, maxHeight: 160, borderRadius: 10, border: "1px solid var(--border)", objectFit: "cover" }}
              />
            </a>
          ))}
        </div>
      )}
      {files.map((a, i) => (
        <div key={i} style={{ fontSize: 12.5, opacity: 0.75, marginBottom: 4 }}>
          <a href={a.url} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>
            📎 {a.filename}
          </a>{" "}
          <span style={{ opacity: 0.6 }}>{formatBytes(a.size)}</span>
        </div>
      ))}
      {text && (
        <div style={{ fontSize: density(dense).userSize, lineHeight: 1.5, fontWeight: 500, color: "var(--hand)", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
          {parity ? linkify(text) : text}
        </div>
      )}
      {stamp && <div className="chat-ts" style={{ color: "var(--ink)" }}>{stamp}</div>}
      <div style={{ height: 1, margin: "7px 0 0 auto", width: "62%", opacity: 0.28, background: "var(--hand)" }} />
    </div>
  );
}

// End of one of the agent's turns — a hairline across the column with the
// time sitting in a break in it.
//
// The point is the SILENT turn. A turn can reach a terminal state having
// produced no text at all (the supervisor's PASS_SKIP path marks it
// complete with an empty response), and until this existed that unlocked
// the composer with nothing new on screen — indistinguishable from the
// agent still working. This rule is the "that turn is over" mark, whether
// or not anything was said.
//
// Built as two rule segments with the text between them rather than one
// rule with a background-colored chip laid over it. Same look, but it makes
// no assumption about what is painted behind the column — the palette is
// swapped by data-hour across eight buckets (globals.css) and the chat
// column has no background of its own, so a chip would have to name a
// colour and would be wrong the first time this list sits on anything but
// bare --bg.
function TurnBoundary({ at }: { at: Date }) {
  const label = formatClock(at);
  return (
    <div
      role="separator"
      aria-label={`End of turn, ${label}`}
      style={{
        // Span the container: every other child is a flex-start/flex-end
        // bubble, this one is the full width of the column.
        alignSelf: "stretch",
        display: "flex",
        alignItems: "center",
        gap: 10,
        // The list's own 20px gap reads as too much air around something
        // this quiet; pull back so the rule sits with the turn it closes.
        margin: "-4px 6px",
      }}
    >
      <span aria-hidden style={{ flex: 1, height: 1, background: "var(--border)" }} />
      {/* Matches the FinalAnswer footer exactly (11.5 / --ink at 0.42) —
          this is secondary text in the same view, not a new voice. */}
      <span
        style={{
          flex: "none",
          fontSize: 11.5,
          lineHeight: 1,
          color: "var(--ink)",
          opacity: 0.42,
          whiteSpace: "nowrap",
        }}
      >
        {label}
      </span>
      <span aria-hidden style={{ flex: 1, height: 1, background: "var(--border)" }} />
    </div>
  );
}

// A slash command and its output — a distinct system slip, not an agent
// bubble (no FinalAnswer prose-measure/markdown, no agent-name footer).
// Monospace throughout so command output (command output / JSON dumps)
// reads as a terminal transcript, same visual language as the rest of chat
// via slip-far + var(--*) tokens.
function SysSlip({
  cmd,
  args,
  output,
  running,
  isError,
  delay,
}: {
  cmd: string;
  args?: string;
  output: string;
  running: boolean;
  isError: boolean;
  delay: number;
}) {
  return (
    <div
      className="anim-liftD66"
      style={{
        animationDelay: `${delay}ms`,
        alignSelf: "flex-start",
        maxWidth: "88%",
        fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
      }}
    >
      <div style={{ fontSize: 11, opacity: 0.55, marginBottom: 4, letterSpacing: ".01em" }}>
        $ /{cmd}
        {args ? ` ${args}` : ""}
      </div>
      <pre
        className="slip-far"
        style={{
          margin: 0,
          padding: "11px 14px",
          borderRadius: 10,
          fontSize: 12.5,
          lineHeight: 1.5,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          color: isError ? "var(--warn)" : "var(--ink)",
          border: "1px solid var(--border)",
        }}
      >
        {running ? "…" : output || "(no output)"}
      </pre>
    </div>
  );
}

function ThinkingBlock({
  text,
  breathing,
  turnStreaming,
  delay,
  seconds,
}: {
  text: string;
  /** Show the pulse dot — the thinking segment itself is still arriving. */
  breathing: boolean;
  /** Whole assistant turn, not just this segment — drives auto-collapse. */
  turnStreaming: boolean;
  delay: number;
  /** Frozen elapsed time once the next event has arrived ("thinking · 4s"). */
  seconds?: number;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const wasStreaming = useRef(turnStreaming);

  // Auto-collapse once the whole turn finishes, not just when this
  // <thinking> segment's closing tag arrives — the answer may still be
  // streaming below it.
  useEffect(() => {
    if (wasStreaming.current && !turnStreaming) setCollapsed(true);
    wasStreaming.current = turnStreaming;
  }, [turnStreaming]);

  return (
    <button
      type="button"
      onClick={() => setCollapsed((c) => !c)}
      className="anim-liftD34 press"
      style={{
        animationDelay: `${delay}ms`,
        alignSelf: "flex-start",
        display: "block",
        textAlign: "left",
        background: "none",
        border: "none",
        color: "inherit",
        font: "inherit",
        cursor: "pointer",
        padding: "2px 0 2px 13px",
        borderLeft: "1.5px solid currentColor",
        fontSize: 13,
        fontStyle: "italic",
        lineHeight: 1.5,
        maxWidth: "88%",
      }}
    >
      <span
        style={{
          fontStyle: "normal",
          fontSize: 9.5,
          letterSpacing: ".13em",
          textTransform: "uppercase",
          opacity: 0.75,
          marginBottom: 5,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        {seconds && seconds >= 1 ? `thinking · ${seconds}s` : "thinking"}
        {breathing && (
          <span
            aria-hidden
            style={{
              width: 5,
              height: 5,
              borderRadius: "50%",
              background: "currentColor",
              animation: "breathe 1.3s ease-in-out infinite",
            }}
          />
        )}
      </span>
      {!collapsed && text}
    </button>
  );
}

function Interstitial({ label, body, delay, dense }: { label: string; body: string; delay: number; dense: boolean }) {
  const d = density(dense);
  return (
    <div
      className="slip-far anim-liftD66"
      style={{
        animationDelay: `${delay}ms`,
        alignSelf: "flex-start",
        maxWidth: "82%",
        padding: d.interstitialPad,
        fontSize: d.interstitialSize,
        lineHeight: 1.45,
      }}
    >
      <div style={{ fontSize: 9.5, letterSpacing: ".13em", textTransform: "uppercase", opacity: 0.7, marginBottom: 4 }}>
        {label}
      </div>
      {body}
    </div>
  );
}

function FinalAnswer({
  children,
  streaming,
  footer,
  delay,
  dense,
  parity,
  header,
  stamp,
  grouped,
}: {
  children: ReactNode;
  streaming: boolean;
  footer?: string;
  delay: number;
  dense: boolean;
  parity?: boolean;
  header?: string;
  stamp?: string;
  grouped?: boolean;
}) {
  const d = density(dense);
  return (
    <div
      // prose-measure: agent prose never stretches past 720px, no matter
      // how wide the window gets. A no-op in the
      // panel, which is narrower than the measure at every width — kept so
      // the two variants aren't structurally different for no reason.
      className={parity ? "slip-near lit anim-lift prose-measure chat-msg" : "slip-near lit anim-lift prose-measure"}
      tabIndex={parity ? 0 : undefined}
      style={{
        animationDelay: `${delay}ms`,
        alignSelf: "flex-start",
        ...(grouped ? { marginTop: -7 } : {}),
        padding: d.answerPad,
        fontSize: d.answerSize,
        lineHeight: 1.5,
        color: "var(--ink)",
      }}
    >
      {header && <div style={{ fontSize: 11.5, opacity: 0.5, marginBottom: 4 }}>{header}</div>}
      <div className="chat-markdown">
        {children}
        {streaming && (
          <span
            aria-hidden
            style={{
              display: "inline-block",
              width: 2,
              height: d.caretHeight,
              verticalAlign: "-3px",
              marginLeft: 3,
              background: "var(--accent)",
              animation: "caret 0.95s step-end infinite",
            }}
          />
        )}
      </div>
      {footer && (
        <div style={{ fontSize: 11.5, opacity: 0.42, marginTop: 8 }}>{footer}</div>
      )}
      {stamp && <div className="chat-ts">{stamp}</div>}
    </div>
  );
}

// Discord's "X is typing…": shown while a turn is in flight and nothing has
// come back yet, so the wait is never a silent empty bubble.
function TypingIndicator({ name, delay }: { name: string; delay: number }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="slip-far anim-lift"
      style={{
        animationDelay: `${delay}ms`,
        alignSelf: "flex-start",
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "8px 13px",
        borderRadius: 14,
        fontSize: 12.5,
        color: "var(--ink)",
      }}
    >
      <span className="typing-dots" aria-hidden>
        <i />
        <i />
        <i />
      </span>
      <span style={{ opacity: 0.7 }}>{name} is working…</span>
    </div>
  );
}

const markdownComponents: Components = {
  a: (props) => (
    <a {...props} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }} />
  ),
  // Inline image/file previews: an agent reply can embed
  // ![caption](/api/files/raw?path=<workspace-relative path under an
  // ALLOWED_ROOT>) and it renders here instead of overflowing at native
  // size. Click opens the full-res original in a new tab; pair with a
  // [⬇ download](/api/files/raw?path=...&download=1) link (raw route
  // route.ts) for a real save-as.
  img: ({ src, alt }) => {
    if (!src || typeof src !== "string") return null;
    return (
      <a href={src} target="_blank" rel="noopener noreferrer" style={{ display: "block", margin: "10px 0" }}>
        <img
          src={src}
          alt={alt ?? ""}
          style={{
            display: "block",
            maxWidth: "100%",
            height: "auto",
            borderRadius: 10,
            border: "1px solid var(--border)",
          }}
        />
        {alt && (
          <span style={{ display: "block", fontSize: 11.5, opacity: 0.5, marginTop: 4 }}>{alt}</span>
        )}
      </a>
    );
  },
  code: ({ className, children, ...props }) => {
    const isBlock = /\n/.test(String(children ?? ""));
    const style = {
      backgroundColor: "var(--surface)",
      border: "1px solid var(--border)",
    };
    if (isBlock) {
      return (
        <code
          className={`block rounded px-3 py-2 my-2 overflow-x-auto font-mono text-xs ${className ?? ""}`}
          style={style}
          {...props}
        >
          {children}
        </code>
      );
    }
    return (
      <code className="rounded px-1 py-0.5 font-mono text-xs" style={style} {...props}>
        {children}
      </code>
    );
  },
  pre: ({ children }) => <>{children}</>,
  ul: (props) => <ul className="list-disc pl-5 my-3 space-y-1" {...props} />,
  ol: (props) => <ol className="list-decimal pl-5 my-3 space-y-1" {...props} />,
  h1: (props) => <h1 className="text-lg font-semibold mt-4 mb-2" {...props} />,
  h2: (props) => <h2 className="text-base font-semibold mt-4 mb-2" {...props} />,
  h3: (props) => <h3 className="text-sm font-semibold mt-3 mb-2" {...props} />,
  p: (props) => <p className="my-3 leading-relaxed" {...props} />,
  blockquote: (props) => (
    <blockquote className="border-l-2 pl-3 my-2" style={{ borderColor: "var(--border-subtle)", opacity: 0.75 }} {...props} />
  ),
  table: ({ children, ...props }) => (
    <div className="overflow-x-auto my-2">
      <table className="border-collapse text-xs w-full" style={{ border: "1px solid var(--border)" }} {...props}>
        {children}
      </table>
    </div>
  ),
  th: (props) => (
    <th className="px-2 py-1 font-semibold text-left" style={{ border: "1px solid var(--border)", backgroundColor: "var(--surface)" }} {...props} />
  ),
  td: (props) => <td className="px-2 py-1" style={{ border: "1px solid var(--border)" }} {...props} />,
  hr: (props) => <hr style={{ borderColor: "var(--border)" }} className="my-3" {...props} />,
};

export default function ChatSurface({ agent: pinnedAgent, variant = "page", discordParity = false }: ChatSurfaceProps) {
  const isPanel = variant === "panel";
  const P = discordParity;
  const d = density(isPanel);
  const { data: agentData } = usePoll<AgentList>("/api/agents", 30000);
  const [agent, setAgent] = useState(pinnedAgent ?? "");
  // Scoped to the agent this surface actually talks to: the dot answers
  // "is the agent I'm talking to busy". Derived from the roster poll above.
  const statusShard = pinnedAgent ?? agent;
  const rosterEntry = agentData?.agents?.find((a) => a.name === statusShard);
  const agentStatus: AgentStatus | undefined = rosterEntry
    ? rosterEntry.state === "PROCESSING"
      ? { busy: true, state: "replying" }
      : { busy: false, state: "idle" }
    : undefined;
  // Same scoping as the status dot. Drives the Send button's
  // working-state and the confirm-to-interrupt gate on submit.
  const isTargetBusy = agent === statusShard && !!agentStatus?.busy;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [statusPill, setStatusPill] = useState<StatusPill>(null);
  const [reloading, setReloading] = useState(false);
  const [reloadMsg, setReloadMsg] = useState<string | null>(null);
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [now, setNow] = useState<Date | null>(null);
  // Confirm-to-interrupt modal.
  // Holds the message text staged for send while the agent is busy — null
  // means the modal is closed. Set on submit when agentStatus.busy is true
  // for this surface's shard; cleared on any of the three resolutions
  // (interrupt / queue / cancel).
  const [busyConfirmText, setBusyConfirmText] = useState<string | null>(null);
  const messagesEnd = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const attachInputRef = useRef<HTMLInputElement>(null);
  const cameraInputRef = useRef<HTMLInputElement>(null);
  const idbRef = useRef<IDBDatabase | null>(null);
  const statusTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastChunkRef = useRef<number>(0);
  // In-flight send count: with parity, sends overlap (queue-while-replying),
  // so `streaming` is "any send still waiting", not a single boolean.
  const streamCount = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);
  const atBottomRef = useRef(true);
  // Set on a local send: follow to the bottom regardless of scroll position.
  const forceBottomRef = useRef(false);
  const [showJump, setShowJump] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const beginStream = () => {
    streamCount.current += 1;
    setStreaming(true);
  };
  const endStream = () => {
    streamCount.current = Math.max(0, streamCount.current - 1);
    if (streamCount.current === 0) setStreaming(false);
  };

  // The picker lists every live agent the account is allowed to reach
  // (/api/agents already trims by permission — this is display shaping
  // only). See lib/sessionRoster.ts for the pure logic (and its tests).
  const sessionOptions: SessionOption[] = agentData?.agents
    ? buildSessionList(agentData.agents)
    : [];
  const agents = sessionOptions.map((o) => o.name);
  const AGENT_LABELS: Record<string, string> = Object.fromEntries(
    sessionOptions.map((o) => [o.name, o.displayName])
  );
  const selectedSession = sessionOptions.find((o) => o.name === agent);
  // The agent this surface talks to, named for the user.
  const agentName = AGENT_LABELS[agent] ?? (agent ? friendlyName(agent) : agent);
  const who = agentName || "The agent";

  // Reconcile a bubble against the server's authoritative row. The SSE
  // stream dies whenever iOS backgrounds the PWA, and the empty bubble it
  // leaves behind used to be persisted to IndexedDB forever. Patches by
  // messageId (not "last message") and only ever grows content.
  // retries > 0 keeps polling while the turn is still running server-side.
  const reconcileMessage = useCallback(
    async (messageId: string, retries = 0): Promise<void> => {
      try {
        const res = await fetch(
          `/api/chat/result?message_id=${encodeURIComponent(messageId)}`
        );
        if (!res.ok) return;
        const data: { response: string; processed: number; turnEndedAt?: string | null } =
          await res.json();
        if (data.response || data.turnEndedAt) {
          setMessages((prev) =>
            prev.map((m) => {
              if (m.role !== "assistant" || m.messageId !== messageId) return m;
              const next = { ...m };
              // Content only ever grows — never clobber a longer local
              // bubble with a shorter server row.
              if (data.response && data.response.length > m.content.length) {
                next.content = data.response;
              }
              // The turn-end stamp is why this poll matters for a SILENT
              // turn: there is no response text to patch in, so without
              // this the reconcile would be a no-op and the boundary would
              // never appear on a stream that died.
              if (data.turnEndedAt && !m.turnEndedAt) next.turnEndedAt = data.turnEndedAt;
              return next;
            })
          );
        }
        // Still queued/in-progress and caller wants us to wait for it.
        if (data.processed < 2 && retries > 0) {
          setTimeout(() => void reconcileMessage(messageId, retries - 1), 3000);
        }
      } catch {
        // transient — a retrying caller will come back around
        if (retries > 0) {
          setTimeout(() => void reconcileMessage(messageId, retries - 1), 3000);
        }
      }
    },
    []
  );

  // Clock in the header — client-only to dodge an SSR/hydration mismatch.
  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(id);
  }, []);

  // Open IndexedDB once
  useEffect(() => {
    openIdb()
      .then((db) => { idbRef.current = db; })
      .catch(() => {}); // graceful degradation if IDB unavailable
  }, []);

  // Set default agent. A pinned surface never picks one: the caller named
  // it, and it stays named even if the roster reports it down — an empty
  // picker on /chat is a "nothing to talk to yet" state, but a panel that
  // silently retargeted itself would be a lie about who answered.
  useEffect(() => {
    if (pinnedAgent) {
      setAgent(pinnedAgent);
      return;
    }
    if (sessionOptions.length > 0 && !agent) setAgent(defaultSessionName(sessionOptions));
  }, [sessionOptions, agent, pinnedAgent]);

  useEffect(() => {
    // block:"start" walks every scrollable ancestor. On /chat that's only
    // the message list — AppShell's isChat branch makes `main` a
    // non-scrolling flex column. Embedded, `main` DOES scroll, and the
    // default would drag the whole host page down on every chunk;
    // block:"nearest" leaves an ancestor alone once the target is visible
    // in it, which after the list scrolls to its own bottom it is.
    if (P) {
      // Chat-app behaviour: follow the conversation only while already at the
      // bottom (or when the newest message is your own send); otherwise leave
      // the scroll position alone and offer a jump-to-latest pill.
      const el = listRef.current;
      if (!el) return;
      const mine = messages[messages.length - 1]?.role === "user";
      if (shouldFollow({ atBottom: atBottomRef.current, forced: forceBottomRef.current, lastIsUser: mine })) {
        el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
        atBottomRef.current = true;
        setShowJump(false);
      } else {
        setShowJump(true);
      }
      return;
    }
    messagesEnd.current?.scrollIntoView(
      isPanel ? { behavior: "smooth", block: "nearest" } : { behavior: "smooth" }
    );
  }, [messages, isPanel, P]);

  // Persist messages to IndexedDB whenever they change
  useEffect(() => {
    if (!agent || !idbRef.current) return;
    idbPut(idbRef.current, `chat:${agent}`, messages).catch(() => {});
  }, [messages, agent]);

  // Load history when agent changes — prefer IndexedDB, fall back to server
  useEffect(() => {
    if (!agent) return;
    let cancelled = false;

    async function load() {
      // Try IndexedDB first
      if (idbRef.current) {
        const cached = await idbGet(idbRef.current, `chat:${agent}`);
        if (cached && cached.length > 0 && !cancelled) {
          setMessages(cached);
          // The cache faithfully preserves bubbles whose stream died before
          // any content arrived. Back-fill them from the server DB.
          for (const m of cached) {
            if (m.role === "assistant" && !m.content && m.messageId) {
              // A still-live turn keeps polling until it lands; anything
              // older gets the single back-fill it always got.
              void reconcileMessage(m.messageId, P && isLiveTurn(m) ? 60 : 0);
            }
          }
          // Turn-end stamps only reach the cache for turns this browser
          // streamed live; anything seeded from server history before this
          // shipped, or run from another device, has none. One history
          // fetch back-fills the lot — it merges turnEndedAt by messageId
          // and touches nothing else, so the partial bubbles the cache is
          // deliberately preserving above are left exactly as they are.
          void (async () => {
            try {
              const res = await fetch(
                `/api/chat/history?agent=${encodeURIComponent(agent)}&limit=50`
              );
              if (!res.ok || cancelled) return;
              const data = await res.json();
              const ends = new Map<string, string>();
              for (const m of data.messages || []) {
                if (m.role === "assistant" && m.messageId && m.turnEndedAt) {
                  ends.set(m.messageId, m.turnEndedAt);
                }
              }
              if (ends.size === 0 || cancelled) return;
              setMessages((prev) =>
                prev.map((m) =>
                  m.role === "assistant" && m.messageId && !m.turnEndedAt && ends.has(m.messageId)
                    ? { ...m, turnEndedAt: ends.get(m.messageId) }
                    : m
                )
              );
            } catch {
              // Best-effort — a missing rule is cosmetic, not a failure.
            }
          })();
          return;
        }
      }

      // Fall back to server history
      try {
        const res = await fetch(
          `/api/chat/history?agent=${encodeURIComponent(agent)}&limit=50`
        );
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const seeded: ChatMessage[] = (data.messages || []).map(
          (m: {
            role: "user" | "assistant";
            content: string;
            ts: string;
            messageId?: string;
            turnEndedAt?: string;
            attachments?: ChatAttachment[];
          }) => ({
            role: m.role,
            content: m.content,
            ts: m.ts,
            messageId: m.messageId,
            turnEndedAt: m.turnEndedAt,
            ...(m.attachments?.length ? { attachments: m.attachments } : {}),
          })
        );
        if (!cancelled) {
          setMessages(seeded);
          if (P) {
            for (const m of seeded) {
              if (m.role === "assistant" && m.messageId && isLiveTurn(m)) {
                void reconcileMessage(m.messageId, 60);
              }
            }
          }
        }
      } catch {
        // ignore
      }
    }

    load();
    return () => { cancelled = true; };
  }, [agent]);

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 24;
    const padding =
      parseFloat(getComputedStyle(ta).paddingTop) +
      parseFloat(getComputedStyle(ta).paddingBottom);
    const max = lineHeight * MAX_TEXTAREA_ROWS + padding;
    ta.style.height = Math.min(ta.scrollHeight, max) + "px";
  }, [input]);

  // Status pill: after 500ms without a chunk, show status hint
  const armStatusTimer = useCallback(() => {
    if (statusTimerRef.current) clearTimeout(statusTimerRef.current);
    statusTimerRef.current = setTimeout(() => {
      // Heuristic: if it's been quiet a while and we're still streaming,
      // show a contextual status hint
      const silenceMs = Date.now() - lastChunkRef.current;
      if (silenceMs >= 500) {
        setStatusPill("saving");
      }
    }, 500);
  }, []);

  function clearStatusTimer() {
    if (statusTimerRef.current) {
      clearTimeout(statusTimerRef.current);
      statusTimerRef.current = null;
    }
    setStatusPill(null);
  }

  async function handleReload() {
    if (!agent || reloading || streaming) return;
    setReloading(true);
    setReloadMsg(`Reloading ${agent}…`);
    try {
      const res = await fetch(`/api/agents/${encodeURIComponent(agent)}/reload`, {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: "Request failed" }));
        setReloadMsg(`Reload failed: ${err.error || res.statusText}`);
      } else {
        setReloadMsg(`${agent} reloaded.`);
        setTimeout(() => setReloadMsg(null), 4000);
      }
    } catch (err) {
      setReloadMsg(`Reload error: ${err instanceof Error ? err.message : "unknown"}`);
    } finally {
      setReloading(false);
    }
  }

  // Clear = fresh session, empty context (destructive — the shard's whole
  // conversation memory goes). Wipes the local bubble history too, since
  // it now describes a conversation the agent no longer remembers.
  async function handleClear() {
    if (!agent || reloading || streaming) return;
    if (!confirm(`Clear ${agent}'s session? Fresh start, all context lost.`)) return;
    setReloading(true);
    setReloadMsg(`Clearing ${agent}…`);
    try {
      const res = await fetch(`/api/agents/${encodeURIComponent(agent)}/clear`, {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: "Request failed" }));
        setReloadMsg(`Clear failed: ${err.error || res.statusText}`);
      } else {
        setMessages([]);
        // Key must match the `chat:${agent}` every other call site uses —
        // it did not, so "clear session" left the local cache intact and the
        // conversation reappeared on the next mount. Pre-dates this file;
        // fixed here because two routes now share it.
        if (idbRef.current) await idbPut(idbRef.current, `chat:${agent}`, []);
        setReloadMsg(`${agent} cleared — fresh session.`);
        setTimeout(() => setReloadMsg(null), 4000);
      }
    } catch (err) {
      setReloadMsg(`Clear error: ${err instanceof Error ? err.message : "unknown"}`);
    } finally {
      setReloading(false);
    }
  }

  // Attach / camera affordances: staged locally only. The chat API doesn't
  // accept attachments yet — TODO wire these into /api/chat's payload once
  // it does; until then this is presentation only, no upload plumbing.
  // Parity surfaces: upload immediately to /api/chat/upload (saved under
  // data/attachments/<date>/); the returned record rides along on send.
  function addFiles(files: File[]) {
    for (const f of files) {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const previewUrl = f.type.startsWith("image/") ? URL.createObjectURL(f) : undefined;
      setPendingAttachments((prev) => [...prev, { id, name: f.name || "pasted-image", size: f.size, status: "uploading", previewUrl }]);
      const form = new FormData();
      form.append("file", f, f.name || `pasted-${id}.png`);
      fetch("/api/chat/upload", { method: "POST", body: form })
        .then(async (res) => {
          if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
          const att: ChatAttachment = await res.json();
          setPendingAttachments((prev) => prev.map((a) => (a.id === id ? { ...a, status: "done", att } : a)));
        })
        .catch(() => {
          setPendingAttachments((prev) => prev.map((a) => (a.id === id ? { ...a, status: "error" } : a)));
        });
    }
  }

  function onFilesSelected(e: ChangeEvent<HTMLInputElement>) {
    const files = e.target.files;
    if (P) {
      if (files && files.length > 0) addFiles(Array.from(files));
      e.target.value = "";
      return;
    }
    if (files && files.length > 0) {
      const next: PendingAttachment[] = Array.from(files).map((f) => ({
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        name: f.name,
        size: f.size,
      }));
      setPendingAttachments((prev) => [...prev, ...next]);
    }
    e.target.value = "";
  }

  function removeAttachment(id: string) {
    setPendingAttachments((prev) => prev.filter((a) => a.id !== id));
  }

  const uploadingCount = pendingAttachments.filter((a) => a.status === "uploading").length;
  const readyCount = pendingAttachments.filter((a) => a.status === "done").length;
  const canSend = P ? (!!input.trim() || readyCount > 0) && uploadingCount === 0 : !!input.trim();

  function onPasteFiles(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    if (!P) return;
    const files = Array.from(e.clipboardData.files);
    if (files.length > 0) {
      e.preventDefault();
      addFiles(files);
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend(e as unknown as FormEvent);
    }
  }

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    if (!canSend || !agent) return;
    // Legacy surfaces block sends mid-reply; parity surfaces route them
    // through the busy-confirm modal below (Interrupt / Queue it).
    if (streaming && !P) return;

    // Busy-interrupt gate: only for the shard this surface is actually
    // pinned to — the same scoping the status dot uses. Sending while idle,
    // or while some other shard is busy, goes straight through as before.
    if (isTargetBusy || (P && streaming)) {
      setBusyConfirmText(input);
      return;
    }

    const userInput = input;
    setInput("");
    await sendMessage(userInput, true);
  }

  // Resolves the busy-confirm modal: "Interrupt him" sends with
  // mentions_agent true (today's only behavior, the deliberate-interrupt
  // steering path); "Queue it" sends with mentions_agent false, landing in
  // message_queue normally for pickup when the current turn ends.
  async function resolveBusyConfirm(mentionsAgent: boolean) {
    const text = busyConfirmText;
    setBusyConfirmText(null);
    if (text === null) return;
    setInput("");
    await sendMessage(text, mentionsAgent);
  }

  async function sendMessage(userInput: string, mentionsAgent: boolean) {
    // Parity surfaces send the uploaded files; legacy surfaces still only
    // stage chips (see onFilesSelected) and clear them on send.
    const atts = P ? pendingAttachments.flatMap((a) => (a.att ? [a.att] : [])) : [];
    const userMsg: ChatMessage = {
      role: "user",
      content: userInput,
      ts: new Date().toISOString(),
      ...(atts.length ? { attachments: atts } : {}),
    };
    if (P) {
      // Chat-app behaviour: your own send always jumps to the bottom and clears the pill.
      forceBottomRef.current = true;
      atBottomRef.current = true;
      setShowJump(false);
    }
    setMessages((prev) => [...prev, userMsg]);
    for (const a of pendingAttachments) if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
    setPendingAttachments([]);
    beginStream();
    let ended = false;
    const finish = () => {
      if (!ended) {
        ended = true;
        endStream();
      }
    };
    lastChunkRef.current = Date.now();

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent,
          content: userInput,
          mentions_agent: mentionsAgent,
          ...(atts.length ? { attachments: atts } : {}),
        }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: "Request failed" }));
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `Error: ${err.error}`, ts: new Date().toISOString() },
        ]);
        finish();
        return;
      }

      const data = await res.json();
      const messageId = data.message_id;

      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "", ts: new Date().toISOString(), messageId },
      ]);

      armStatusTimer();

      // Dedup: track the last chunk we rendered to avoid double-rendering
      // on React StrictMode double-invoke or reconnect.
      let lastRenderedLength = 0;

      const eventSource = new EventSource(
        `/api/chat/stream?message_id=${messageId}`
      );

      eventSource.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.done) {
          eventSource.close();
          clearStatusTimer();
          finish();
          // The turn is over — stamp it so a boundary rule is drawn under
          // it. This fires on EVERY terminal status the stream reports
          // (complete / crashed / skipped / timeout), because from here
          // they are the same fact: the composer is about to unlock, and
          // the user needs to see that this turn is what just ended, even
          // if it never produced a word.
          setMessages((prev) =>
            prev.map((m) =>
              m.role === "assistant" && m.messageId === messageId && !m.turnEndedAt
                ? { ...m, turnEndedAt: new Date().toISOString() }
                : m
            )
          );
          // Surface a non-complete terminal status (crashed / skipped / ...)
          // instead of letting it render like a clean finish.
          const note = terminalStatusMessage(payload.status, payload.error);
          if (note) {
            setMessages((prev) =>
              prev.map((m) =>
                m.role === "assistant" && m.messageId === messageId ? { ...m, terminalNote: note } : m
              )
            );
          }
          // Belt-and-braces: if buffering ate the chunk events, the server
          // row still has the full text — patch the bubble from it.
          void reconcileMessage(messageId);
        } else if (payload.event) {
          // Typed mid-turn event (thinking / interstitial / tool). Progress
          // is a real sign of life — reset the status-pill heuristic too.
          lastChunkRef.current = Date.now();
          clearStatusTimer();
          armStatusTimer();
          const ev = { ...(payload.event as TurnEvent), arrivedAt: Date.now() };
          setMessages((prev) => {
            const next = [...prev];
            // By messageId, not "last message": with parity, overlapping
            // sends mean the newest bubble is not necessarily this turn's.
            const idx = next.findIndex((m) => m.role === "assistant" && m.messageId === messageId);
            if (idx < 0) return prev;
            const last = next[idx];
            const events = last.events ?? [];
            // Dedup by seq — StrictMode double-invoke / reconnect can replay.
            if (events.some((e) => e.seq === ev.seq)) return prev;
            next[idx] = { ...last, events: [...events, ev] };
            return next;
          });
        } else if (payload.chunk) {
          lastChunkRef.current = Date.now();
          clearStatusTimer();
          armStatusTimer();

          setMessages((prev) => {
            const next = [...prev];
            const idx = next.findIndex((m) => m.role === "assistant" && m.messageId === messageId);
            if (idx < 0) return prev;
            const last = next[idx];
            // Dedup: only append if this chunk would extend our known content
            const newContent = last.content + payload.chunk;
            if (newContent.length <= lastRenderedLength) return prev;
            lastRenderedLength = newContent.length;
            next[idx] = { ...last, content: newContent };
            return next;
          });
        }
      };

      eventSource.onerror = () => {
        eventSource.close();
        clearStatusTimer();
        finish();
        // Stream died (iOS backgrounding, proxy hiccup) but the agent is
        // likely still working. Poll the result row until it completes —
        // 60 tries × 3s covers a 3-minute turn.
        void reconcileMessage(messageId, 60);
      };
    } catch (err) {
      clearStatusTimer();
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: `Connection error: ${err instanceof Error ? err.message : "unknown"}`,
          ts: new Date().toISOString(),
        },
      ]);
      finish();
    }
  }

  const attachmentCaption = pendingAttachments.length
    ? P
      ? `${pendingAttachments.length} file${pendingAttachments.length > 1 ? "s" : ""} · ${formatBytes(
          pendingAttachments.reduce((sum, a) => sum + a.size, 0)
        )}${uploadingCount ? " · uploading…" : ""}`
      : `${pendingAttachments.length} file${pendingAttachments.length > 1 ? "s" : ""} · ${formatBytes(
          pendingAttachments.reduce((sum, a) => sum + a.size, 0)
        )} · will be read, not stored`
    : null;

  // Sender grouping (parity): a header + time only at the start of a run of
  // same-sender messages; followers sit tight under it and show their time on
  // hover/tap. "Visible" mirrors what the list actually renders.
  const visibleRole: (ChatMessage["role"] | null)[] = messages.map((m) => {
    if (m.role === "sys") return "sys";
    if (m.role === "user") return "user";
    const hidden = !m.content.trim() || isPassResponse(m.content, isLiveTurn(m));
    return !hidden || isLiveTurn(m) || (m.events?.length ?? 0) > 0 ? "assistant" : null;
  });
  const runStart = (i: number): boolean => {
    const role = visibleRole[i];
    for (let j = i - 1; j >= 0; j--) {
      if (visibleRole[j]) return visibleRole[j] !== role;
    }
    return true;
  };

  // One inline list: thinking and interstitials sit IN the conversation
  // column at every width.
  const messageView = (() => {
    const main: ReactNode[] = [];
    // Index in `main` of the most recently pushed turn boundary, so a run of
    // silent turns collapses instead of stacking rules. See pushBoundary().
    let lastBoundaryAt = -1;

    // Draw the rule that closes a turn. If the previous rule is still the
    // last thing in the list — nothing was rendered between the two turns,
    // which is exactly what a run of silent turns looks like — replace it
    // rather than stacking a second one, so the single remaining rule
    // carries the LATER time and reads as "…and it's still quiet, as of
    // now" instead of a ladder of hairlines.
    const pushBoundary = (key: string, at: Date) => {
      const node = <TurnBoundary key={key} at={at} />;
      if (lastBoundaryAt === main.length - 1 && lastBoundaryAt >= 0) {
        main[lastBoundaryAt] = node;
        return;
      }
      main.push(node);
      lastBoundaryAt = main.length - 1;
    };

    messages.forEach((msg, i) => {
      const delay = staggerDelay(i);
      const isLastStreaming = P
        ? isLiveTurn(msg)
        : streaming && i === messages.length - 1 && msg.role === "assistant";
      const start = P ? runStart(i) : true;

      if (msg.role === "user") {
        const clock = formatClock(parseServerTs(msg.ts));
        // Attachment-only sends store a "(attachment)" placeholder for the
        // agent's benefit; don't echo it back as the user's words.
        const text = msg.attachments?.length && msg.content === "(attachment)" ? "" : msg.content;
        main.push(
          <UserLine
            key={i}
            text={text}
            delay={delay}
            dense={isPanel}
            parity={P}
            header={P && start ? `You · ${clock}` : undefined}
            stamp={P && !start ? clock : undefined}
            attachments={msg.attachments}
            grouped={P && !start}
          />
        );
        return;
      }

      if (msg.role === "sys") {
        const running = streaming && i === messages.length - 1;
        main.push(
          <SysSlip
            key={i}
            cmd={msg.cmd ?? ""}
            args={msg.args}
            output={msg.content}
            running={running}
            isError={!!msg.sysError}
            delay={delay}
          />
        );
        return;
      }

      const { thinking, body, open } = splitThinking(msg.content);
      const finalTrimmed = body.trim();
      // Typed mid-turn events from the supervisor pump. The pump records the
      // final answer's text block as an interstitial too (it can't know a
      // block is final until the turn ends) — drop any interstitial whose
      // content IS the final body so the answer never renders twice.
      const events = (msg.events ?? []).filter((ev) => {
        // Final answer's own text block, recorded before it was known final.
        if (ev.kind === "interstitial" && finalTrimmed && ev.content.trim() === finalTrimmed) return false;
        // Content-less thinking is a live presence signal (the harness
        // strips thinking text from transcripts) — pulse while streaming,
        // nothing to keep once the turn is done.
        if (ev.kind === "thinking" && !ev.content.trim() && !isLastStreaming) return false;
        return true;
      });
      // The literal "PASS" an agent sends to skip an ambient message is never a bubble —
      // including the half-streamed "PA" while it is still arriving.
      const passHidden = isPassResponse(body, isLastStreaming);
      const showInterstitial =
        isLastStreaming && !body && !!statusPill && events.length === 0 && !P;
      const showTyping = P && isLastStreaming && !body.trim() && events.length === 0 && thinking === null;
      // A completed turn whose response was never captured (supervisor
      // recovery gap) renders as nothing rather than an empty slip —
      // The user line above it still shows, so the gap is visible without a ghost bubble.
      const showFinal = body.length > 0 && !passHidden;
      // parseServerTs, not a bare new Date(): msg.ts is message_queue's
      // created_at for anything seeded from history, which is UTC with no
      // zone marker and parses as local — this footer has been reading
      // hours ahead for every message that came back from the server.
      const footer = !isLastStreaming && !P
        ? `${AGENT_LABELS[agent] ?? agent} · ${formatClock(parseServerTs(msg.ts))}`
        : undefined;
      const clock = formatClock(parseServerTs(msg.ts));

      events.forEach((ev, j) => {
        const isLastEvent = j === events.length - 1;
        let node: ReactNode;
        if (ev.kind === "thinking") {
          const next = events[j + 1];
          const seconds =
            ev.arrivedAt && next?.arrivedAt
              ? Math.round((next.arrivedAt - ev.arrivedAt) / 1000)
              : undefined;
          node = (
            <ThinkingBlock
              key={`ev-${i}-${ev.seq}`}
              text={ev.content}
              breathing={isLastStreaming && isLastEvent && !body}
              turnStreaming={isLastStreaming}
              delay={delay}
              seconds={seconds}
            />
          );
        } else {
          node = (
            <Interstitial
              key={`ev-${i}-${ev.seq}`}
              label={ev.kind === "tool" ? "checking" : "working"}
              body={ev.content}
              delay={delay}
              dense={isPanel}
            />
          );
        }
        main.push(node);
      });
      if (thinking !== null) {
        const node = (
          <ThinkingBlock
            key={`think-${i}`}
            text={thinking}
            breathing={isLastStreaming && open}
            turnStreaming={isLastStreaming}
            delay={delay}
          />
        );
        main.push(node);
      }
      if (showInterstitial && statusPill) {
        const node = (
          <Interstitial
            key={`int-${i}`}
            label={INTERSTITIAL[statusPill].label}
            body={INTERSTITIAL[statusPill].body}
            delay={delay}
            dense={isPanel}
          />
        );
        main.push(node);
      }
      if (showTyping) {
        main.push(<TypingIndicator key={`typing-${i}`} name={agentName} delay={delay} />);
      }
      if (showFinal) {
        main.push(
          <FinalAnswer
            key={`final-${i}`}
            streaming={isLastStreaming}
            footer={footer}
            delay={delay}
            dense={isPanel}
            parity={P}
            header={P && start ? `${agentName} · ${clock}` : undefined}
            stamp={P && !start ? clock : undefined}
            grouped={P && !start}
          >
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {body}
            </ReactMarkdown>
          </FinalAnswer>
        );
      }

      if (msg.terminalNote) {
        main.push(
          <div
            key={`terminal-note-${i}`}
            role="alert"
            data-testid="terminal-status-note"
            style={{ fontSize: 12, color: "var(--warn)", padding: "4px 0" }}
          >
            {msg.terminalNote}
          </div>
        );
      }

      // Close the turn. Driven purely by turnEndedAt, so it lands under a
      // turn that said nothing just as reliably as one that answered — the
      // whole reason this exists. A turn still streaming has no stamp yet
      // and gets no rule.
      if (msg.turnEndedAt) {
        const at = parseServerTs(msg.turnEndedAt);
        if (!Number.isNaN(at.getTime())) pushBoundary(`turn-end-${i}`, at);
      }
    });
    return { main };
  })();

  return (
    <div className="flex flex-1 min-h-0 min-w-0">
      <div
        className="flex flex-col flex-1 min-h-0 min-w-0"
        style={{ position: "relative", ...(P && dragOver ? { outline: "2px dashed var(--accent)", outlineOffset: -4 } : {}) }}
        onDragOver={P ? (e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDragOver(true); } } : undefined}
        onDragLeave={P ? (e) => { if (e.currentTarget === e.target) setDragOver(false); } : undefined}
        onDrop={
          P
            ? (e) => {
                setDragOver(false);
                if (e.dataTransfer.files.length > 0) {
                  e.preventDefault();
                  addFiles(Array.from(e.dataTransfer.files));
                }
              }
            : undefined
        }
      >
      {/* main (AppShell) no longer supplies this on /chat — see AppShell's
          isChat branch — because main itself must not scroll here. Embedded
          it's the other way round: the host page takes AppShell's ordinary
          branch, which already lays down this exact spacer above the page,
          so a second one here would be a double notch gutter. */}
      {!isPanel && <div className="pt-safe lg:hidden" aria-hidden />}
      {/* Header — agent selector doubles as the picker; still a real <select>
          under the hood so switching agents keeps working. Pinned: a
          sibling of flex-1-min-h-0 message list below, never scrolls. */}
      <div className="flex-shrink-0" style={{ padding: d.headerPad }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, minWidth: 0 }}>
            {/* Status dot next to the agent name, coloured by activity. */}
            {(() => {
              const dot =
                agent === statusShard && agentStatus
                  ? STATUS_DOT[agentStatus.state ?? (agentStatus.busy ? "batch" : "idle")]
                  : null;
              return (
                <span
                  aria-hidden
                  title={dot ? dot.label(agentStatus?.detail) : undefined}
                  style={{
                    width: 9,
                    height: 9,
                    borderRadius: "50%",
                    flex: "none",
                    // idle keeps the familiar accent orange, just dim and
                    // still — a grey dot here would read as "broken".
                    background: dot && dot.pulse ? dot.color : "var(--accent)",
                    opacity: dot && !dot.pulse ? 0.45 : 1,
                    animation:
                      dot && !dot.pulse ? "none" : "halo 2.8s ease-out infinite",
                  }}
                />
              );
            })()}
            {pinnedAgent ? (
              // Pinned: the name, and no way to leave it. Same weight and
              // tracking as the select it replaces, so the header reads as
              // the same header minus an affordance rather than as a
              // different design.
              <div
                style={{
                  color: "var(--ink)",
                  fontSize: d.titleSize,
                  fontWeight: 600,
                  letterSpacing: "-0.015em",
                  minWidth: 0,
                  overflow: "hidden",
                  whiteSpace: "nowrap",
                  textOverflow: "ellipsis",
                }}
              >
                {agentName}
              </div>
            ) : (
              <SessionPicker
                options={sessionOptions}
                selected={agent}
                titleSize={d.titleSize}
                maxWidth={isPanel ? "100%" : "44vw"}
                onSelect={(name) => {
                  setAgent(name);
                  setMessages([]);
                }}
              />
            )}
          </div>
          <div style={{ display: "flex", alignItems: "center", minWidth: 0, flexShrink: 1, gap: 10 }}>
            {agent && <ConversationMetricsBadge agent={agent} />}
            <span style={{ fontSize: 12, opacity: 0.5, flexShrink: 0, color: "var(--ink)" }}>
              {now ? formatClock(now) : ""}
            </span>
          </div>
        </div>
        {/* Secondary toolbar — reload / clear / busy state. Not in the
            handoff mock (a static screen doesn't need dev tooling) but
            these are real operator affordances that stay. */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: d.toolbarGap,
            marginTop: 6,
            minHeight: 20,
            // Four operator affordances plus a busy line don't fit on one
            // row of a 400px rail. Wrapping is panel-only: /chat has never
            // wrapped this and a wrap there would be a layout change.
            flexWrap: isPanel ? "wrap" : undefined,
            rowGap: isPanel ? 6 : undefined,
          }}
        >
          <button
            type="button"
            onClick={handleReload}
            disabled={reloading || streaming || !agent}
            className="press"
            style={{ background: "none", border: "none", padding: 0, fontSize: 12, opacity: 0.5, color: "var(--ink)", cursor: "pointer" }}
          >
            {reloading ? "reloading…" : "↻ reload"}
          </button>
          <button
            type="button"
            onClick={handleClear}
            disabled={reloading || streaming || !agent}
            className="press"
            style={{ background: "none", border: "none", padding: 0, fontSize: 12, opacity: 0.5, color: "var(--ink)", cursor: "pointer" }}
          >
            ✕ clear session
          </button>
          {reloadMsg && <span style={{ fontSize: 12, opacity: 0.5, color: "var(--ink)" }}>{reloadMsg}</span>}
          {agent === statusShard && agentStatus && (() => {
            const dot = STATUS_DOT[agentStatus.state ?? (agentStatus.busy ? "batch" : "idle")];
            return (
              <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, opacity: dot.pulse ? 0.7 : 0.4, color: "var(--ink)" }}>
                <span
                  aria-hidden
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "50%",
                    background: dot.color,
                    animation: dot.pulse ? "breathe 1.3s ease-in-out infinite" : "none",
                  }}
                />
                {dot.label(agentStatus.detail)}
              </span>
            );
          })()}
        </div>
      </div>

      {/* Speech hierarchy: final answers, interstitials, thinking, user lines */}
      {(() => {
        const list = (
          <div
            ref={listRef}
            onScroll={
              P
                ? (e) => {
                    const el = e.currentTarget;
                    const next = onScrollState(
                      { atBottom: atBottomRef.current, forced: forceBottomRef.current },
                      isAtBottom(el)
                    );
                    atBottomRef.current = next.atBottom;
                    forceBottomRef.current = next.forced;
                    if (next.atBottom) setShowJump(false);
                  }
                : undefined
            }
            // User scrolling on purpose cancels a pending forced follow.
            onWheel={P ? () => { forceBottomRef.current = false; } : undefined}
            onTouchMove={P ? () => { forceBottomRef.current = false; } : undefined}
            // Late-loading images grow the list; keep following if we were.
            onLoadCapture={
              P
                ? () => {
                    const el = listRef.current;
                    if (el && (forceBottomRef.current || atBottomRef.current)) {
                      el.scrollTo({ top: el.scrollHeight });
                    }
                  }
                : undefined
            }
            className="flex-1 overflow-auto flex flex-col min-h-0"
            style={{ gap: d.listGap, padding: d.listPad, overscrollBehavior: "contain" }}
          >
            {messageView.main}
            <div ref={messagesEnd} />
          </div>
        );
        if (!P) return list;
        return (
          <div className="flex flex-col flex-1 min-h-0" style={{ position: "relative" }}>
            {list}
            {showJump && (
              <button
                type="button"
                onClick={() => {
                  const el = listRef.current;
                  if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
                  atBottomRef.current = true;
                  setShowJump(false);
                }}
                className="press slip-near"
                style={{
                  position: "absolute",
                  bottom: 10,
                  left: "50%",
                  transform: "translateX(-50%)",
                  padding: "6px 14px",
                  borderRadius: 999,
                  fontSize: 12.5,
                  fontWeight: 600,
                  color: "var(--ink)",
                  border: "1px solid var(--border)",
                  cursor: "pointer",
                  zIndex: 10,
                }}
              >
                ↓ Jump to latest
              </button>
            )}
          </div>
        );
      })()}

      {/* Composer — pinned like the header. chat-composer-pad (globals.css)
          clears the TabShelf overlay on phone; plain safe-area at lg+. It
          is page-only: an embedding page takes AppShell's ordinary branch, which
          already appends its own shelf spacer under the page, so the panel
          would be padding clear of a shelf that is already cleared. */}
      <div
        className={isPanel ? "flex-shrink-0" : "flex-shrink-0 chat-composer-pad"}
        style={{
          paddingTop: d.composerPadTop,
          paddingLeft: d.composerGutter,
          paddingRight: d.composerGutter,
        }}
      >
        {attachmentCaption && (
          <div style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8, margin: "0 4px 9px" }}>
            {pendingAttachments.map((att) => (
              <span
                key={att.id}
                className="slip-far"
                style={{ display: "flex", alignItems: "center", gap: 7, padding: "6px 10px 6px 7px", borderRadius: 10, fontSize: 12 }}
              >
                {P && att.previewUrl ? (
                  <img
                    src={att.previewUrl}
                    alt=""
                    style={{ width: 26, height: 26, borderRadius: 6, flex: "none", objectFit: "cover" }}
                  />
                ) : (
                <span
                  aria-hidden
                  style={{
                    width: 26,
                    height: 26,
                    borderRadius: 6,
                    flex: "none",
                    background:
                      "repeating-linear-gradient(135deg, color-mix(in srgb, var(--ink) 16%, transparent) 0 3px, transparent 3px 6px)",
                  }}
                />
                )}
                <span style={{ opacity: 0.75 }}>{att.name}</span>
                {att.status === "uploading" && <span style={{ opacity: 0.5 }}>…</span>}
                {att.status === "error" && <span style={{ color: "var(--warn)" }}>failed</span>}
                <button
                  type="button"
                  onClick={() => removeAttachment(att.id)}
                  aria-label={`Remove ${att.name}`}
                  className="press"
                  style={{ background: "none", border: "none", padding: 0, opacity: 0.45, color: "inherit", cursor: "pointer" }}
                >
                  ✕
                </button>
              </span>
            ))}
            <span style={{ fontSize: 11.5, opacity: 0.4 }}>{attachmentCaption}</span>
          </div>
        )}

        <div style={{ position: "relative" }}>
          <form
            onSubmit={handleSend}
            className="slip-near lit"
            style={{ display: "flex", alignItems: "center", gap: 10, borderRadius: 999, padding: d.composerPad, minHeight: d.composerMinHeight }}
          >
          <button
            type="button"
            onClick={() => attachInputRef.current?.click()}
            aria-label="Attach a file"
            className="press"
            style={{
              width: 36,
              height: 36,
              marginLeft: -8,
              borderRadius: "50%",
              flex: "none",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 22,
              fontWeight: 300,
              opacity: 0.6,
              border: "1px solid color-mix(in srgb, var(--ink) 22%, transparent)",
              background: "none",
              color: "inherit",
              cursor: "pointer",
            }}
          >
            +
          </button>
          <button
            type="button"
            onClick={() => cameraInputRef.current?.click()}
            aria-label="Use camera"
            className="press"
            style={{
              width: 36,
              height: 36,
              borderRadius: "50%",
              flex: "none",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              opacity: 0.6,
              border: "1px solid color-mix(in srgb, var(--ink) 22%, transparent)",
              background: "none",
              color: "inherit",
              cursor: "pointer",
            }}
          >
            <svg width="17" height="17" viewBox="0 0 17 17" fill="none" stroke="currentColor" strokeWidth="1.4">
              <rect x="1.4" y="3.6" width="14.2" height="10.4" rx="2.2" />
              <circle cx="8.5" cy="8.8" r="2.9" />
            </svg>
          </button>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
            }}
            onKeyDown={handleKeyDown}
            onPaste={onPasteFiles}
            placeholder={`Message ${P ? agentName : agent || "agent"}…`}
            disabled={streaming && !P}
            rows={1}
            className="flex-1 resize-none focus:outline-none disabled:opacity-50"
            style={{
              background: "none",
              border: "none",
              color: "var(--ink)",
              fontSize: 17, // ≥17px — iOS zooms the viewport below 16px
              lineHeight: 1.4,
              padding: "15px 0",
            }}
          />
          <button
            type="submit"
            disabled={P ? !canSend : streaming || !input.trim()}
            aria-label={isTargetBusy ? `${who} is working…` : "Send"}
            title={isTargetBusy ? `${who} is working — sending will ask you to confirm` : undefined}
            style={{
              width: isTargetBusy ? "auto" : 40,
              height: 40,
              minWidth: 40,
              padding: isTargetBusy ? "0 14px" : undefined,
              borderRadius: 999,
              flex: "none",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 6,
              fontSize: isTargetBusy ? 13 : 18,
              fontWeight: isTargetBusy ? 600 : undefined,
              background: isTargetBusy ? "var(--warn)" : "var(--accent)",
              color: isTargetBusy ? "var(--on-warn, #1a1a1a)" : "var(--on-accent)",
              border: "none",
              boxShadow: `0 0 22px color-mix(in srgb, ${isTargetBusy ? "var(--warn)" : "var(--accent)"} 40%, transparent)`,
              opacity: (P ? !canSend : streaming || !input.trim()) ? 0.5 : 1,
              cursor: (P ? !canSend : streaming || !input.trim()) ? "default" : "pointer",
            }}
            className="press"
          >
            {isTargetBusy ? `${who} is working…` : "↑"}
          </button>
          </form>
        </div>
        <input ref={attachInputRef} type="file" multiple hidden onChange={onFilesSelected} />
        <input ref={cameraInputRef} type="file" accept="image/*" capture="environment" hidden onChange={onFilesSelected} />
      </div>
      </div>
      {busyConfirmText !== null && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={`${who} is working`}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 1000,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 20,
            background: "color-mix(in srgb, black 55%, transparent)",
          }}
          onClick={() => setBusyConfirmText(null)}
        >
          <div
            className="slip-near lit"
            onClick={(e) => e.stopPropagation()}
            style={{
              width: "100%",
              maxWidth: 440,
              borderRadius: 16,
              padding: 20,
              display: "flex",
              flexDirection: "column",
              gap: 14,
            }}
          >
            <div style={{ fontSize: 16, fontWeight: 700 }}>
              {`${who} is replying — interrupt or queue?`}
            </div>
            <div
              style={{
                fontSize: 14,
                opacity: 0.75,
                maxHeight: 120,
                overflowY: "auto",
                whiteSpace: "pre-wrap",
                borderLeft: "2px solid color-mix(in srgb, var(--ink) 22%, transparent)",
                paddingLeft: 10,
              }}
            >
              {busyConfirmText || (readyCount > 0 ? "(attachment)" : "")}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <button
                type="button"
                onClick={() => resolveBusyConfirm(true)}
                className="press"
                style={{
                  padding: "10px 14px",
                  borderRadius: 10,
                  border: "1px solid color-mix(in srgb, var(--warn) 55%, transparent)",
                  background: "color-mix(in srgb, var(--warn) 16%, transparent)",
                  color: "var(--ink)",
                  textAlign: "left",
                  cursor: "pointer",
                }}
              >
                <div style={{ fontWeight: 600, fontSize: 14 }}>{P ? "Interrupt" : "Interrupt him"}</div>
                <div style={{ fontSize: 12.5, opacity: 0.7 }}>
                  {P ? "Sends now. The current turn is abandoned" : "Sends now. His current turn is abandoned"} — use this for a real stop-and-do-this-instead.
                </div>
              </button>
              <button
                type="button"
                onClick={() => resolveBusyConfirm(false)}
                className="press"
                style={{
                  padding: "10px 14px",
                  borderRadius: 10,
                  border: "1px solid color-mix(in srgb, var(--accent) 55%, transparent)",
                  background: "color-mix(in srgb, var(--accent) 16%, transparent)",
                  color: "var(--ink)",
                  textAlign: "left",
                  cursor: "pointer",
                }}
              >
                <div style={{ fontWeight: 600, fontSize: 14 }}>{P ? `Queue it — let ${who} finish first` : "Queue it — let him finish first"}</div>
                <div style={{ fontSize: 12.5, opacity: 0.7 }}>
                  {P ? "Lands after the current turn ends" : "Lands after his current turn ends"}, same as sending while idle. Use this for check-ins.
                </div>
              </button>
              <button
                type="button"
                onClick={() => setBusyConfirmText(null)}
                className="press"
                style={{
                  padding: "8px 14px",
                  borderRadius: 10,
                  border: "1px solid color-mix(in srgb, var(--ink) 18%, transparent)",
                  background: "none",
                  color: "var(--ink)",
                  opacity: 0.7,
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
