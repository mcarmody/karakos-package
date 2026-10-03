import { NextRequest, NextResponse } from "next/server";
import {
  agentFetch,
  AGENT_NAME_RE,
  invalidAgentNameResponse,
  isAuthenticated,
  unauthorizedResponse,
} from "@/lib/api";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  if (!AGENT_NAME_RE.test(name)) {
    return invalidAgentNameResponse();
  }

  try {
    const response = await agentFetch(`/agents/${name}/queue`);
    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to fetch queue" },
      { status: 500 }
    );
  }
}

export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  if (!AGENT_NAME_RE.test(name)) {
    return invalidAgentNameResponse();
  }

  try {
    const body = await request.json();
    const { messageId } = body;

    if (!messageId) {
      return NextResponse.json({ error: "messageId required" }, { status: 400 });
    }
    // The agent-server takes the queue row's integer id (`id` in the GET body).
    if (!/^\d+$/.test(String(messageId))) {
      return NextResponse.json({ error: "messageId must be a queue row id" }, { status: 400 });
    }

    const response = await agentFetch(`/agents/${name}/queue/${messageId}`, {
      method: "DELETE",
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to delete queue item" },
      { status: 500 }
    );
  }
}
