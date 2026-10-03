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

  if (!AGENT_NAME_RE.test(name)) {
    return invalidAgentNameResponse();
  }

  try {
    const response = await agentFetch(`/agents/${name}/reset`, {
      method: "POST",
    });

    if (!response.ok) {
      return NextResponse.json(
        { error: `Failed to reset agent: ${response.statusText}` },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to reset agent" },
      { status: 500 }
    );
  }
}
