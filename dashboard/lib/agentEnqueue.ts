import { agentFetch } from "@/lib/api";

/**
 * Package-profile send path: POST /message on the agent-server
 * (docs/package-backend-contract.md). The agent-server answers 202 for both
 * "queued" and "duplicate"; callers (app/api/chat) treat 200 as success, so
 * 202 is reported as 200.
 */
export async function enqueueForAgent(
  payload: Record<string, unknown>
): Promise<{ status: number; body: Record<string, unknown>; via: string }> {
  const res = await agentFetch("/message", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let body: Record<string, unknown> = {};
  try {
    body = (await res.json()) as Record<string, unknown>;
  } catch {
    // keep status only
  }
  return { status: res.status === 202 ? 200 : res.status, body, via: "agent-server" };
}

export function newMessageId(prefix = "dash"): string {
  return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 65536)}`;
}
