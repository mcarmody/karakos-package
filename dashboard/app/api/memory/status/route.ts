import { NextRequest } from "next/server";
import { fetchGraphStatus } from "@/lib/memoryBackend";
import { memoryGuard, memoryResponse } from "../guard";

// GET /graph/status, package profile only.
export async function GET(request: NextRequest) {
  const denied = memoryGuard(request);
  if (denied) return denied;
  return memoryResponse(await fetchGraphStatus());
}
