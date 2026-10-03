import { agentFetch } from "@/lib/api";

/**
 * Reload an agent's subprocess via the agent-server. Neutral: no systemctl,
 * tmux or pty code.
 */
export async function reloadAgent(
  name: string
): Promise<{ ok: true; data: unknown } | { error: string; status: number }> {
  try {
    const response = await agentFetch(`/agents/${name}/reload`, {
      method: "POST",
    });

    if (!response.ok) {
      return {
        error: `Failed to reload agent: ${response.statusText}`,
        status: response.status,
      };
    }

    return { ok: true, data: await response.json() };
  } catch {
    return { error: "Failed to reload agent", status: 500 };
  }
}
