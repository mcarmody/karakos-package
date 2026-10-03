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
