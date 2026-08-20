"use client";

import { useState, useEffect, useRef, useCallback, FormEvent, KeyboardEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { usePoll } from "@/lib/hooks";

interface AgentList {
  // /api/agents returns { agents: [...] }, an array of { name, state, ... }
  // objects -- not a dict keyed by name. This was previously typed as
  // Record<string, {state}> and read via Object.keys(agentData.agents),
  // which on an array yields numeric indices ("0", "1", ...) instead of
  // agent names. The dropdown looked populated but was silently wrong.
  agents: { name: string; state: string; label?: string; dashboard_chat?: boolean }[];
}

// Typed mid-turn event written by bin/agent-server.py's read_agent_response
// and relayed by /api/chat/stream as `{event: {...}}` SSE payloads. Thinking
// renders collapsible and dim, interstitials (text-before-the-final-answer,
// and tool calls) render as their own subdued rows — neither is ever the
// turn's conclusion; the final answer is the accumulating `content` field,
// same as before this existed.
interface TurnEvent {
  kind: "thinking" | "interstitial" | "tool";
  content: string;
  seq: number;
  /** Client-side arrival time (ms) — drives the frozen "thinking · Ns" label. */
  arrivedAt?: number;
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  ts: string;
  // Terminal status of the turn that produced this message, as reported by
  // /api/chat/stream. Absent on user messages and on history seeded from
  // /api/chat, which does not record it.
  status?: string;
  error?: string;
  // message_queue.message_id for this turn — lets the client reconcile a
  // bubble against /api/chat/result if the SSE stream dies mid-turn.
  messageId?: string;
  events?: TurnEvent[];
}

const MAX_TEXTAREA_ROWS = 8;

// The one terminal status that means the agent finished its turn. Every other
// value the stream can send -- crashed, skipped, timeout, error, unknown:<n>
// (see dashboard/app/api/chat/stream/route.ts) -- ended the turn early, and
// the text already rendered above the banner is a partial answer.
const STATUS_COMPLETE = "complete";

// Client-side only: EventSource.onerror, i.e. the connection dropped before
// any terminal status arrived. Distinct from the server's "error", which the
// server was still alive enough to send.
const STATUS_DISCONNECTED = "disconnected";

/** Banner copy for a non-complete terminal status. */
function terminalStatusMessage(status: string, error?: string): string {
  switch (status) {
    case "crashed":
      return "The agent crashed mid-turn. The response above is partial.";
    case "skipped":
      return "This turn was skipped — the agent never processed it.";
    case "timeout":
      return "The stream timed out after 5 minutes. The agent may still be working; reload to see the stored response.";
    case STATUS_DISCONNECTED:
      return "Lost connection to the server before the turn finished. The response above is partial.";
    case "error":
      return error
        ? `The stream failed: ${error}`
        : "The stream failed before the turn finished.";
    default:
      // unknown:<n> — a status the server knows about and this client does
      // not. Show it rather than swallowing it.
      return `The turn ended with an unexpected status: ${status}.`;
  }
}

/** Collapsible, dimmed row for a "thinking" turn event. Auto-collapses once
 * the whole turn finishes streaming, not just when this segment ends -- the
 * answer may still be arriving below it. */
function ThinkingRow({
  text,
  breathing,
  turnStreaming,
  seconds,
}: {
  text: string;
  /** Show the pulse dot -- this thinking segment itself is still arriving. */
  breathing: boolean;
  /** Whole assistant turn, not just this segment -- drives auto-collapse. */
  turnStreaming: boolean;
  /** Frozen elapsed time once the next event has arrived ("thinking · 4s"). */
  seconds?: number;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const wasStreaming = useRef(turnStreaming);

  useEffect(() => {
    if (wasStreaming.current && !turnStreaming) setCollapsed(true);
    wasStreaming.current = turnStreaming;
  }, [turnStreaming]);

  return (
    <button
      type="button"
      onClick={() => setCollapsed((c) => !c)}
      className="anim-liftD34 block text-left max-w-[88%] mb-2 border-l-2 border-gray-700 pl-3 py-1 text-xs italic text-gray-500 bg-transparent cursor-pointer"
    >
      <span className="flex items-center gap-1.5 not-italic text-[10px] uppercase tracking-wide opacity-75 mb-1">
        {seconds && seconds >= 1 ? `thinking · ${seconds}s` : "thinking"}
        {breathing && (
          <span
            aria-hidden
            className="inline-block w-1.5 h-1.5 rounded-full bg-current"
            style={{ animation: "breathe 1.3s ease-in-out infinite" }}
          />
        )}
      </span>
      {!collapsed && text}
    </button>
  );
}

/** Subdued row for an "interstitial" (mid-turn text) or "tool" event --
 * progress only, never the turn's conclusion. */
function InterstitialRow({ label, body }: { label: string; body: string }) {
  return (
    <div className="anim-liftD66 mb-2 max-w-[82%] rounded-lg border border-gray-800 bg-gray-950 px-3 py-2 text-xs text-gray-400">
      <div className="text-[10px] uppercase tracking-wide opacity-70 mb-1">{label}</div>
      {body}
    </div>
  );
}

export default function ChatPage() {
  const { data: agentData } = usePoll<AgentList>("/api/agents", 30000);
  const [agent, setAgent] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [reloadMsg, setReloadMsg] = useState<string | null>(null);
  const [openingTerminal, setOpeningTerminal] = useState(false);
  const messagesEnd = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const agents = agentData?.agents ? agentData.agents.map((a) => a.name) : [];

  // Set default agent
  useEffect(() => {
    if (agents.length > 0 && !agent) {
      setAgent(agents[0]);
    }
  }, [agents, agent]);

  useEffect(() => {
    messagesEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Reconcile a bubble against the server's authoritative row. The SSE
  // stream can die before a turn finishes (backgrounded mobile browser,
  // reverse-proxy hiccup) and leave the bubble empty even though the row in
  // message_queue holds the full text. Patches by messageId (not "last
  // message") and only ever grows content. retries > 0 keeps polling while
  // the turn is still running server-side.
  const reconcileMessage = useCallback(
    async (messageId: string, retries = 0): Promise<void> => {
      try {
        const res = await fetch(
          `/api/chat/result?message_id=${encodeURIComponent(messageId)}`
        );
        if (!res.ok) return;
        const data: { response: string; processed: number } = await res.json();
        if (data.response) {
          setMessages((prev) =>
            prev.map((m) =>
              m.role === "assistant" &&
              m.messageId === messageId &&
              data.response.length > m.content.length
                ? { ...m, content: data.response }
                : m
            )
          );
        }
        // Still queued/in-progress and the caller wants us to wait for it.
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

  async function handleOpenTerminal() {
    if (!agent || openingTerminal) return;
    setOpeningTerminal(true);
    setReloadMsg(`Opening ${agent} in Terminal…`);
    try {
      const res = await fetch(`/api/agents/${encodeURIComponent(agent)}/open-terminal`, {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: "Request failed" }));
        setReloadMsg(`Terminal failed: ${err.error || res.statusText}`);
      } else {
        setReloadMsg(`${agent} opened in Terminal.`);
        setTimeout(() => setReloadMsg(null), 4000);
      }
    } catch (err) {
      setReloadMsg(`Terminal error: ${err instanceof Error ? err.message : "unknown"}`);
    } finally {
      setOpeningTerminal(false);
    }
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
        setReloadMsg(`${agent} reloaded — session preserved.`);
        setTimeout(() => setReloadMsg(null), 4000);
      }
    } catch (err) {
      setReloadMsg(`Reload error: ${err instanceof Error ? err.message : "unknown"}`);
    } finally {
      setReloading(false);
    }
  }

  // Seed messages from server-side history when an agent is selected. The
  // chat page kept no record across reloads even though message_queue stored
  // every turn — pulling the last N entries restores continuity.
  useEffect(() => {
    if (!agent) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(
          `/api/chat/history?agent=${encodeURIComponent(agent)}&limit=50`
        );
        if (!res.ok) return;
        const data = await res.json();
        if (cancelled) return;
        const seeded: ChatMessage[] = (data.messages || []).map(
          (m: { role: "user" | "assistant"; content: string; ts: string }) => ({
            role: m.role,
            content: m.content,
            ts: m.ts,
          })
        );
        setMessages(seeded);
      } catch {
        // ignore — empty chat is acceptable failure mode
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [agent]);

  // Auto-resize textarea up to MAX_TEXTAREA_ROWS lines.
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

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift-Enter inserts newline. Respect IME composition so
    // multi-keystroke input methods (Japanese, Chinese, Korean) don't fire
    // a send while a candidate is being chosen.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend(e as unknown as FormEvent);
    }
  }

  async function handleSend(e: FormEvent) {
    e.preventDefault();
    if (!input.trim() || !agent || streaming) return;

    const userMsg: ChatMessage = { role: "user", content: input, ts: new Date().toISOString() };
    setMessages((prev) => [...prev, userMsg]);
    const userInput = input;
    setInput("");
    setStreaming(true);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent, content: userInput }),
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ error: "Request failed" }));
        setMessages((prev) => [...prev, { role: "assistant", content: `Error: ${err.error}`, ts: new Date().toISOString() }]);
        setStreaming(false);
        return;
      }

      const data = await res.json();
      const messageId = data.message_id;

      // Add placeholder for streaming response
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "", ts: new Date().toISOString(), messageId },
      ]);

      // Subscribe to SSE stream
      const eventSource = new EventSource(`/api/chat/stream?message_id=${messageId}`);

      // Stamp a terminal status onto the assistant message currently being
      // streamed. Guarded on role so a race that appended a user message
      // first cannot mark the wrong bubble as crashed.
      const markLastAssistant = (status: string, error?: string) => {
        setMessages((prev) =>
          prev.map((m, i) =>
            i === prev.length - 1 && m.role === "assistant"
              ? { ...m, status, error }
              : m
          )
        );
      };

      // A terminal status may arrive with the response still empty (an agent
      // that crashed before writing a byte). Tracked so onerror can tell a
      // real transport drop from the close that follows a normal finish.
      let sawTerminal = false;

      eventSource.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.done) {
          // Previously this discarded payload.status, so a crash rendered
          // identically to a clean finish -- the user saw a partial response
          // and assumed the agent was done (#64, server side fixed in #56).
          sawTerminal = true;
          markLastAssistant(payload.status ?? STATUS_COMPLETE, payload.error);
          eventSource.close();
          setStreaming(false);
          // Belt-and-braces: if buffering ate the chunk events, the server
          // row still has the full text -- patch the bubble from it.
          void reconcileMessage(messageId);
        } else if (payload.event) {
          // Typed mid-turn event (thinking / interstitial / tool).
          const ev = { ...(payload.event as TurnEvent), arrivedAt: Date.now() };
          setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (!last || last.role !== "assistant") return prev;
            const events = last.events ?? [];
            // Dedup by seq -- StrictMode double-invoke / reconnect can replay.
            if (events.some((e) => e.seq === ev.seq)) return prev;
            next[next.length - 1] = { ...last, events: [...events, ev] };
            return next;
          });
        } else if (payload.chunk) {
          setMessages((prev) =>
            prev.map((m, i) =>
              i === prev.length - 1 && m.role === "assistant"
                ? { ...m, content: m.content + payload.chunk }
                : m
            )
          );
        }
      };

      eventSource.onerror = () => {
        // EventSource fires onerror on a normal close too. Only surface a
        // banner if no terminal status ever arrived -- otherwise this would
        // overwrite the real status with "disconnected" on every clean turn.
        if (!sawTerminal) {
          markLastAssistant(STATUS_DISCONNECTED);
        }
        eventSource.close();
        setStreaming(false);
        // Stream died (mobile backgrounding, proxy hiccup) but the agent is
        // likely still working. Poll the result row until it completes --
        // 60 tries x 3s covers a 3-minute turn.
        void reconcileMessage(messageId, 60);
      };
    } catch (err) {
      setMessages((prev) => [...prev, {
        role: "assistant",
        content: `Connection error: ${err instanceof Error ? err.message : "unknown"}`,
        ts: new Date().toISOString(),
      }]);
      setStreaming(false);
    }
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-4 mb-4 flex-shrink-0">
        <h1 className="text-2xl font-semibold">Chat</h1>
        <select
          value={agent}
          onChange={(e) => { setAgent(e.target.value); setMessages([]); }}
          className="px-3 py-2 bg-gray-900 border border-gray-800 rounded text-gray-100 focus:outline-none focus:border-gray-600"
        >
          {agents.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={handleReload}
          disabled={reloading || streaming || !agent}
          title="Bounce subprocess, preserve session — picks up SYSTEM_PROMPT / persona / MCP changes"
          className="px-3 py-2 bg-gray-900 hover:bg-gray-800 disabled:opacity-50 border border-gray-800 rounded text-gray-300 text-sm transition-colors"
        >
          {reloading ? "Reloading…" : "↻ Reload"}
        </button>
        <button
          type="button"
          onClick={handleOpenTerminal}
          disabled={openingTerminal || !agent}
          title="Open this agent in a Terminal window (macOS only) — REPL with slash commands, mirrors into this chat log"
          className="px-3 py-2 bg-gray-900 hover:bg-gray-800 disabled:opacity-50 border border-gray-800 rounded text-gray-300 text-sm transition-colors"
        >
          {openingTerminal ? "Opening…" : "⌘ Terminal"}
        </button>
        {reloadMsg && (
          <span className="text-xs text-gray-400">{reloadMsg}</span>
        )}
      </div>

      <div className="flex-1 overflow-auto py-2 mb-4 min-h-0">
        {messages.map((msg, i) => {
          const isLastStreaming =
            streaming && i === messages.length - 1 && msg.role === "assistant";

          if (msg.role === "user") {
            return (
              <div key={i} className="p-3 mb-2 rounded-lg bg-gray-950">
                <strong className="text-xs block mb-1 text-gray-100">You</strong>
                <p className="text-sm whitespace-pre-wrap text-gray-300">
                  {msg.content}
                </p>
              </div>
            );
          }

          // Typed mid-turn events from the pump above. It records the final
          // answer's own text block as an interstitial too -- it can't know
          // a block is final until the turn ends -- so drop any interstitial
          // whose content IS the final body, or the answer renders twice.
          const finalTrimmed = msg.content.trim();
          const events = (msg.events ?? []).filter((ev) => {
            if (ev.kind === "interstitial" && finalTrimmed && ev.content.trim() === finalTrimmed) {
              return false;
            }
            // Content-less thinking is a live presence signal (some builds
            // strip thinking text from the transcript) -- pulse while
            // streaming, nothing to keep once the turn is done.
            if (ev.kind === "thinking" && !ev.content.trim() && !isLastStreaming) {
              return false;
            }
            return true;
          });

          // A completed turn whose response was never captured (a recovery
          // gap) renders as nothing rather than an empty slip -- but a
          // crash/timeout/error banner still needs somewhere to show even
          // when the response text is empty.
          const hasProblem = !!msg.status && msg.status !== STATUS_COMPLETE;
          const showFinal = msg.content.length > 0 || hasProblem;

          return (
            <div key={i}>
              {events.map((ev, j) => {
                if (ev.kind === "thinking") {
                  const next = events[j + 1];
                  const seconds =
                    ev.arrivedAt && next?.arrivedAt
                      ? Math.round((next.arrivedAt - ev.arrivedAt) / 1000)
                      : undefined;
                  return (
                    <ThinkingRow
                      key={`ev-${i}-${ev.seq}`}
                      text={ev.content}
                      breathing={isLastStreaming && j === events.length - 1 && !msg.content}
                      turnStreaming={isLastStreaming}
                      seconds={seconds}
                    />
                  );
                }
                return (
                  <InterstitialRow
                    key={`ev-${i}-${ev.seq}`}
                    label={ev.kind === "tool" ? "checking" : "working"}
                    body={ev.content}
                  />
                );
              })}
              {showFinal && (
              <div className="p-3 mb-2 rounded-lg bg-gray-900 border border-gray-800">
              <strong className="text-xs block mb-1 text-blue-400">{agent}</strong>
                <div className="text-sm text-gray-300 chat-markdown">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      a: (props) => (
                        <a
                          {...props}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-blue-400 underline hover:text-blue-300"
                        />
                      ),
                      code: ({ className, children, ...props }) => {
                        const isBlock = /\n/.test(String(children ?? ""));
                        if (isBlock) {
                          return (
                            <code
                              className={`block bg-gray-950 border border-gray-800 rounded px-3 py-2 my-2 overflow-x-auto font-mono text-xs ${className ?? ""}`}
                              {...props}
                            >
                              {children}
                            </code>
                          );
                        }
                        return (
                          <code
                            className="bg-gray-950 border border-gray-800 rounded px-1 py-0.5 font-mono text-xs"
                            {...props}
                          >
                            {children}
                          </code>
                        );
                      },
                      pre: ({ children }) => <>{children}</>,
                      ul: (props) => (
                        <ul className="list-disc pl-5 my-3 space-y-1" {...props} />
                      ),
                      ol: (props) => (
                        <ol className="list-decimal pl-5 my-3 space-y-1" {...props} />
                      ),
                      h1: (props) => (
                        <h1 className="text-lg font-semibold mt-4 mb-2" {...props} />
                      ),
                      h2: (props) => (
                        <h2 className="text-base font-semibold mt-4 mb-2" {...props} />
                      ),
                      h3: (props) => (
                        <h3 className="text-sm font-semibold mt-3 mb-2" {...props} />
                      ),
                      p: (props) => (
                        <p className="my-3 leading-relaxed" {...props} />
                      ),
                      blockquote: (props) => (
                        <blockquote
                          className="border-l-2 border-gray-700 pl-3 my-2 text-gray-400"
                          {...props}
                        />
                      ),
                      table: (props) => (
                        <table
                          className="my-2 border-collapse border border-gray-800 text-xs"
                          {...props}
                        />
                      ),
                      th: (props) => (
                        <th
                          className="border border-gray-800 px-2 py-1 bg-gray-950 font-semibold text-left"
                          {...props}
                        />
                      ),
                      td: (props) => (
                        <td className="border border-gray-800 px-2 py-1" {...props} />
                      ),
                      hr: (props) => (
                        <hr className="border-gray-800 my-3" {...props} />
                      ),
                    }}
                  >
                    {msg.content}
                  </ReactMarkdown>
                  {isLastStreaming && (
                    <span className="opacity-50 inline-block">▌</span>
                  )}
                  {msg.status && msg.status !== STATUS_COMPLETE && (
                    <div
                      role="alert"
                      className="mt-2 flex items-start gap-2 rounded border border-red-900 bg-red-950/50 px-3 py-2 text-xs text-red-300"
                    >
                      <span aria-hidden="true">⚠</span>
                      <span>{terminalStatusMessage(msg.status, msg.error)}</span>
                    </div>
                  )}
                </div>
              </div>
              )}
            </div>
          );
        })}
        <div ref={messagesEnd} />
      </div>

      <form
        onSubmit={handleSend}
        className="flex gap-2 pt-2 border-t border-gray-800 items-end flex-shrink-0"
      >
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={`Message ${agent || "agent"}...  (Shift-Enter for newline)`}
          disabled={streaming}
          rows={1}
          className="flex-1 px-3 py-2 bg-gray-900 border border-gray-800 rounded text-gray-100 text-sm leading-6 resize-none focus:outline-none focus:border-gray-600 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={streaming || !input.trim()}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 text-white rounded transition-colors"
        >
          Send
        </button>
      </form>
    </div>
  );
}
