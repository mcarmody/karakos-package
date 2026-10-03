/**
 * Dashboard chat attachments + PASS filtering. Pure/IO-light helpers shared by
 * /api/chat/upload, /api/chat/attachment, /api/chat and /api/chat/history.
 *
 * Shape is the one the Discord relay writes into message_queue.attachments:
 *   [{"filename","size","content_type","url","local_path"}]
 * with files under <WORKSPACE_ROOT>/data/attachments/<YYYY-MM-DD>/. `local_path`
 * is what the agent reads; `url` is a dashboard-served URL for the browser.
 */

import { realpathSync } from "fs";
import { basename, join, resolve, sep } from "path";
import { WORKSPACE_ROOT } from "@/lib/db";

export const ATTACHMENTS_ROOT = join(WORKSPACE_ROOT, "data", "attachments");
export const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
export const MAX_ATTACHMENTS_PER_MESSAGE = 8;

export { isPassResponse, type ChatAttachment } from "@/lib/chatMessage";
import type { ChatAttachment } from "@/lib/chatMessage";

/** Strip anything path-like / odd from a user-supplied filename. */
export function safeFilename(name: string): string {
  const base = basename(String(name || "").replace(/\\/g, "/"));
  const cleaned = base.replace(/[^A-Za-z0-9._ -]/g, "_").replace(/^\.+/, "").trim();
  return (cleaned || "file").slice(0, 120);
}

export function attachmentUrl(localPath: string): string {
  const rel = localPath.startsWith(ATTACHMENTS_ROOT + sep)
    ? localPath.slice(ATTACHMENTS_ROOT.length + 1)
    : localPath;
  return `/api/chat/attachment?path=${encodeURIComponent(rel)}`;
}

/**
 * Resolve `p` (absolute local_path, or relative to ATTACHMENTS_ROOT) to a real
 * file path inside ATTACHMENTS_ROOT, or null. A client-supplied local_path is
 * otherwise a read-any-file primitive for the agent, so /api/chat revalidates.
 */
export function resolveAttachmentPath(p: string): string | null {
  if (!p || p.includes("\0")) return null;
  try {
    const root = realpathSync(ATTACHMENTS_ROOT);
    const real = realpathSync(resolve(ATTACHMENTS_ROOT, p));
    return real.startsWith(root + sep) ? real : null;
  } catch {
    return null;
  }
}

/** Validate/normalize the client's attachments array; drops anything invalid. */
export function sanitizeAttachments(raw: unknown): ChatAttachment[] {
  if (!Array.isArray(raw)) return [];
  const out: ChatAttachment[] = [];
  for (const a of raw.slice(0, MAX_ATTACHMENTS_PER_MESSAGE)) {
    if (!a || typeof a !== "object") continue;
    const rec = a as Record<string, unknown>;
    const local = typeof rec.local_path === "string" ? resolveAttachmentPath(rec.local_path) : null;
    if (!local) continue;
    out.push({
      filename: safeFilename(typeof rec.filename === "string" ? rec.filename : basename(local)),
      size: typeof rec.size === "number" && rec.size >= 0 ? rec.size : 0,
      content_type:
        typeof rec.content_type === "string" && rec.content_type ? rec.content_type : "application/octet-stream",
      url: attachmentUrl(local),
      local_path: local,
    });
  }
  return out;
}

/** Parse message_queue.attachments (JSON text) defensively. */
export function parseAttachmentsColumn(raw: string | null | undefined): ChatAttachment[] {
  if (!raw) return [];
  try {
    const v = JSON.parse(raw);
    if (!Array.isArray(v)) return [];
    return v
      .filter((a) => a && typeof a === "object" && typeof a.local_path === "string")
      .map((a) => ({
        filename: String(a.filename ?? basename(a.local_path)),
        size: Number(a.size) || 0,
        content_type: String(a.content_type ?? "application/octet-stream"),
        // Discord-written rows carry a CDN url; ours carry a dashboard url.
        // Either way prefer serving from local disk through the dashboard.
        url: attachmentUrl(a.local_path),
        local_path: a.local_path,
      }));
  } catch {
    return [];
  }
}
