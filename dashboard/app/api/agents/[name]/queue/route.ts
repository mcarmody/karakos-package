import { NextRequest, NextResponse } from "next/server";
import { agentFetch, isAuthenticated, unauthorizedResponse } from "@/lib/api";

// Repointed (#151): these called /queue/{name} and /queue/{name}/{id}, which
// agent-server.py has never registered. Both now use the routes added in the
// same change, namespaced under the agent like every other per-agent verb:
//   GET    /agents/{name}/queue            -> { agent, messages: [...] }
//   DELETE /agents/{name}/queue/{id}       -> cancel one queued message
//
// POST /agents/{name}/flush already existed but does not cover the DELETE
// case: it drops the agent's whole backlog, and the modal's row-level "x"
// cancels exactly one message and leaves the rest queued.

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  try {
    const response = await agentFetch(
      `/agents/${encodeURIComponent(name)}/queue`
    );
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
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

  try {
    const body = await request.json();
    const { messageId } = body;

    if (messageId === undefined || messageId === null || messageId === "") {
      return NextResponse.json({ error: "messageId required" }, { status: 400 });
    }

    const agentPath = encodeURIComponent(name);
    const idPath = encodeURIComponent(String(messageId));
    const response = await agentFetch(`/agents/${agentPath}/queue/${idPath}`, {
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
