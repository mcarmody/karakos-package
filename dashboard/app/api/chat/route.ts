import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, sessionUser, unauthorizedResponse } from "@/lib/api";
import { agentAllowed, permissionsFor } from "@/lib/permissions";
import { enqueueForAgent } from "@/lib/agentEnqueue";
import { sanitizeAttachments } from "@/lib/chatAttachments";

export async function POST(request: NextRequest) {
  const session = request.cookies.get("karakos_session")?.value;
  if (!isAuthenticated(session)) {
    return unauthorizedResponse();
  }

  const user = sessionUser(session);
  const perms = permissionsFor(user);

  try {
    const body = await request.json();
    const { agent, content, mentions_agent } = body;

    // Attachments (same shape the chat relay writes). Content may be empty
    // when an image is sent alone; the agent still needs a text line.
    const attachments = sanitizeAttachments(body.attachments);
    const text: string =
      typeof content === "string" && content.trim()
        ? content
        : attachments.length
          ? "(attachment)"
          : "";

    if (!agent || !text) {
      return NextResponse.json(
        { error: "Missing agent or content" },
        { status: 400 }
      );
    }

    // Per-account agent allowlist: middleware can't see the POST body, so
    // the target check lives here. UI hiding (the filtered /api/agents
    // picker) is courtesy — this is the boundary.
    if (!agentAllowed(perms, agent)) {
      return NextResponse.json({ error: "Forbidden" }, { status: 403 });
    }

    // Send to whatever the session picker chose, literally.
    const targetAgent = agent;

    // Restricted accounts speak as themselves, not as the owner.
    const restricted = perms.agents !== "*";
    const ownerName = restricted && user ? user : process.env.OWNER_NAME || "User";
    const ownerId = restricted ? "0" : process.env.OWNER_DISCORD_ID || "0";

    const messageId = `dash-${Date.now()}-${Math.floor(Math.random() * 65536)}`;

    const payload = {
      agent: targetAgent,
      channel: "dashboard",
      channel_id: "0", // Silent — no chat-platform post
      server: "dashboard",
      author: ownerName,
      author_id: ownerId,
      is_bot: false,
      content: text,
      message_id: messageId,
      // Sent as an ARRAY, not a string: the agent-server serialises this field
      // itself, and a pre-stringified value would be double-encoded.
      ...(attachments.length ? { attachments } : {}),
      // Steering-interrupt gate. Default true preserves prior behavior for any
      // caller that doesn't send the field; ChatSurface sends it explicitly
      // based on the confirm-to-interrupt modal.
      mentions_agent: typeof mentions_agent === "boolean" ? mentions_agent : true,
    };

    // Sent through the agent-server (lib/agentEnqueue.ts).
    const { status, body: data } = await enqueueForAgent(payload);
    if (status !== 200) {
      return NextResponse.json(data, { status: status || 502 });
    }
    return NextResponse.json({ ...data, message_id: messageId });
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to send message" },
      { status: 500 }
    );
  }
}
