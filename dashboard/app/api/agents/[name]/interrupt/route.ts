import { NextRequest, NextResponse } from "next/server";
import { agentFetch, isAuthenticated, unauthorizedResponse } from "@/lib/api";

// Repointed (#151): this used to POST /interrupt with the agent in the body.
// agent-server.py has never registered a bare /interrupt — the real route is
// POST /agents/{name}/interrupt, alongside kill and flush. Every "Interrupt"
// click 404'd and the modal reported "Interrupt failed" on a healthy server.
export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  try {
    const response = await agentFetch(
      `/agents/${encodeURIComponent(name)}/interrupt`,
      { method: "POST" }
    );

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to interrupt agent" },
      { status: 500 }
    );
  }
}
