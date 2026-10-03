import { NextRequest, NextResponse } from "next/server";
import { mkdirSync, writeFileSync } from "fs";
import { join } from "path";
import { randomBytes } from "crypto";
import { isAuthenticated, unauthorizedResponse } from "@/lib/api";
import {
  ATTACHMENTS_ROOT,
  MAX_ATTACHMENT_BYTES,
  attachmentUrl,
  safeFilename,
  type ChatAttachment,
} from "@/lib/chatAttachments";

export const dynamic = "force-dynamic";

// Saves one dropped/pasted/picked file under data/attachments/<date>/ — the
// same tree the Discord relay uses — and returns the attachment record the
// client then passes to /api/chat. Upload alone grants nothing: /api/chat
// re-validates local_path before it ever reaches an agent.
export async function POST(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }
  try {
    const form = await request.formData();
    const file = form.get("file");
    if (!(file instanceof File)) {
      return NextResponse.json({ error: "Missing file" }, { status: 400 });
    }
    if (file.size > MAX_ATTACHMENT_BYTES) {
      return NextResponse.json({ error: "File too large (25 MB max)" }, { status: 413 });
    }
    const date = new Date().toISOString().slice(0, 10);
    const dir = join(ATTACHMENTS_ROOT, date);
    mkdirSync(dir, { recursive: true });
    const filename = safeFilename(file.name);
    const localPath = join(dir, `${randomBytes(4).toString("hex")}-${filename}`);
    writeFileSync(localPath, Buffer.from(await file.arrayBuffer()));
    const att: ChatAttachment = {
      filename,
      size: file.size,
      content_type: file.type || "application/octet-stream",
      url: attachmentUrl(localPath),
      local_path: localPath,
    };
    return NextResponse.json(att);
  } catch (error) {
    return NextResponse.json(
      { error: `Upload failed: ${error instanceof Error ? error.message : "unknown"}` },
      { status: 500 }
    );
  }
}
