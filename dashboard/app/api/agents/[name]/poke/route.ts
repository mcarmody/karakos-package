import { NextRequest, NextResponse } from "next/server";
import { isAuthenticated, unauthorizedResponse } from "@/lib/api";
import { enqueueForAgent, newMessageId } from "@/lib/agentEnqueue";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  try {
    const body = await request.json();
    const { message, channel } = body;

    if (!message) {
      return NextResponse.json({ error: "message required" }, { status: 400 });
    }

    // Sent through the agent-server, like /api/chat.
    const { status, body: data } = await enqueueForAgent({
      agent: name,
      channel: channel || "general",
      channel_id: "dashboard",
      author: "dashboard",
      author_id: "dashboard",
      content: message,
      message_id: newMessageId("dashboard-poke"),
      mentions_agent: true,
    });
    return NextResponse.json(data, { status: status || 502 });
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to send message" },
      { status: 500 }
    );
  }
}
