import { packageAdapter } from "@/lib/agentStatus.package";
import type { AgentStatusAdapter } from "@/lib/agentStatus";

export async function getAgentStatusAdapter(): Promise<AgentStatusAdapter> {
  return packageAdapter;
}
