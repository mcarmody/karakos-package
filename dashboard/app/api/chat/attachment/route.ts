import { NextRequest, NextResponse } from "next/server";
import { readFileSync } from "fs";
import { basename } from "path";
import { isAuthenticated, unauthorizedResponse } from "@/lib/api";
import { resolveAttachmentPath } from "@/lib/chatAttachments";

export const dynamic = "force-dynamic";

const TYPES: Record<string, string> = {
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
  webp: "image/webp", heic: "image/heic", pdf: "application/pdf", txt: "text/plain",
};

// Serves files from data/attachments only (realpath-checked). Images inline;
// everything else forced to download so an uploaded .html can't run here.
export async function GET(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }
  const p = request.nextUrl.searchParams.get("path") ?? "";
  const real = resolveAttachmentPath(p);
  if (!real) return NextResponse.json({ error: "Not found" }, { status: 404 });
  const ext = (real.split(".").pop() || "").toLowerCase();
  const type = TYPES[ext] ?? "application/octet-stream";
  const inline = type.startsWith("image/") && type !== "image/svg+xml";
  return new Response(readFileSync(real), {
    headers: {
      "Content-Type": type,
      "Content-Disposition": `${inline ? "inline" : "attachment"}; filename="${basename(real).replace(/"/g, "")}"`,
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "private, max-age=86400",
    },
  });
}
