import { NextRequest, NextResponse } from "next/server";
import {
  agentFetch,
  AGENT_NAME_RE,
  invalidAgentNameResponse,
  isAuthenticated,
  unauthorizedResponse,
} from "@/lib/api";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ name: string }> }
) {
  const { name } = await params;

  if (!isAuthenticated(request.cookies.get("karakos_session")?.value)) {
    return unauthorizedResponse();
  }

  try {
    // The agent-server serves POST /agents/{name}/interrupt (no body,
    // docs/package-backend-contract.md).
    if (!AGENT_NAME_RE.test(name)) return invalidAgentNameResponse();
    const response = await agentFetch(`/agents/${name}/interrupt`, { method: "POST" });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to interrupt agent" },
      { status: 500 }
    );
  }
}
