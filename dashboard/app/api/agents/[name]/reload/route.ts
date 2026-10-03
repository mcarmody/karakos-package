import { NextRequest, NextResponse } from "next/server";
import {
  AGENT_NAME_RE,
  invalidAgentNameResponse,
  isAuthenticated,
  unauthorizedResponse,
} from "@/lib/api";
import { reloadAgent } from "@/lib/agentReload";

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

  const result = await reloadAgent(name);
  if ("error" in result) {
    return NextResponse.json({ error: result.error }, { status: result.status });
  }
  return NextResponse.json(result.data);
}
