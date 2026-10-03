import { NextRequest, NextResponse } from "next/server";
import { fetchEntity } from "@/lib/memoryBackend";
import { memoryGuard, memoryResponse } from "../../guard";

// GET /graph/entities/{id}.
export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const denied = memoryGuard(request);
  if (denied) return denied;
  const { id } = await params;
  if (!/^\d{1,12}$/.test(id)) return NextResponse.json({ error: "id must be an integer" }, { status: 400 });
  return memoryResponse(await fetchEntity(id));
}
