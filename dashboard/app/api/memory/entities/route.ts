import { NextRequest, NextResponse } from "next/server";
import { fetchEntities, parseMemoryFilter } from "@/lib/memoryBackend";
import { memoryGuard, memoryResponse } from "../guard";

// GET /graph/entities, validated passthrough.
export async function GET(request: NextRequest) {
  const denied = memoryGuard(request);
  if (denied) return denied;
  const parsed = parseMemoryFilter(request.nextUrl.searchParams, "entities");
  if (!parsed.ok) return NextResponse.json({ error: parsed.error }, { status: 400 });
  return memoryResponse(await fetchEntities(parsed.query));
}
