/**
 * Client-safe chat message helpers (no fs / db imports — ChatSurface bundles
 * this). Server-side attachment handling lives in lib/chatAttachments.ts.
 */

export interface ChatAttachment {
  filename: string;
  size: number;
  content_type: string;
  url: string;
  local_path: string;
}

/**
 * An agent replies the literal `PASS` to ambient messages. Never a bubble.
 * `partial` also hides a still-streaming prefix ("P", "PA"...) so the word
 * doesn't flash in before it completes.
 */
export function isPassResponse(text: string | null | undefined, partial = false): boolean {
  const t = (text ?? "").trim();
  if (!t) return false;
  if (t === "PASS") return true;
  return partial && "PASS".startsWith(t);
}

/**
 * Terminal status sent by /api/chat/stream on `{done: true, status}`.
 * "complete" is the success path and renders nothing; every other status
 * (crashed / skipped / forbidden / timeout / error / `unknown:<n>`) gets a
 * visible note under the turn so a partial answer is not read as a finished one.
 */
export const STATUS_COMPLETE = "complete";

export function terminalStatusMessage(status: string | null | undefined, error?: string | null): string | null {
  if (!status || status === STATUS_COMPLETE) return null;
  const detail = error ? ` (${error})` : "";
  switch (status) {
    case "crashed":
      return `The agent crashed during this turn — the reply may be incomplete.${detail}`;
    case "skipped":
      return "This message was skipped — the agent did not answer it.";
    case "forbidden":
      return "You don't have access to this agent's reply.";
    case "timeout":
      return "The reply timed out — it may still arrive.";
    case "error":
      return `The stream failed — the reply may be incomplete.${detail}`;
    default:
      return `The turn ended with an unexpected status: ${status}.${detail}`;
  }
}
