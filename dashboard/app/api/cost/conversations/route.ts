import { NextRequest, NextResponse } from "next/server";
import { agentFetch, isAuthenticated, unauthorizedResponse } from "@/lib/api";

// Conversation metrics: cost_events rolled up by (agent, session_id) —
// a conversation is one context window, not one turn. See
// handle_cost_conversations in bin/agent-server.py.
export async function GET(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  try {
    const agent = request.nextUrl.searchParams.get("agent");
    const path = agent
      ? `/cost/conversations?agent=${encodeURIComponent(agent)}`
      : "/cost/conversations";
    const response = await agentFetch(path);

    if (!response.ok) {
      return NextResponse.json(
        { error: `Failed to fetch conversation data: ${response.statusText}` },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to fetch conversation data" },
      { status: 500 }
    );
  }
}
