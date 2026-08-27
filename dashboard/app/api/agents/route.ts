import { NextRequest, NextResponse } from "next/server";
import { agentFetch, isAuthenticated, unauthorizedResponse } from "@/lib/api";

// This route reshapes agent-server data into { agents: AgentInfo[] } — an
// ARRAY (see tests/test_dashboard_agents_contract.py, which enforces that
// no consumer reads it with Object.keys()).
//
// It used to source that from GET /status, which agent-server.py has never
// actually registered (bin/agent-server.py's router.add_get calls list
// /health, /agents, /cost/{agent}, /usage — no /status). Every request here
// 404'd, so the agent picker on /chat silently rendered empty. Two routes
// that DO exist between them carry everything the old shape promised:
// /health's per-agent dict has live state/alive/queue_depth/session_id,
// /agents' array has the agents.json config (model, dashboard_chat, ...).
export async function GET(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value || "")) {
    return unauthorizedResponse();
  }

  try {
    const [healthRes, configRes] = await Promise.all([
      agentFetch("/health"),
      agentFetch("/agents"),
    ]);
    const health = await healthRes.json();
    const configData = await configRes.json();

    const healthByName: Record<string, any> = health.agents || {};
    const configList: any[] = configData.agents || [];

    const agents = configList.map((cfg) => {
      const info = healthByName[cfg.name] || {};
      return {
        name: cfg.name,
        state: info.state || cfg.state || "UNKNOWN",
        // Human-friendly display name and dashboard-chat eligibility, read
        // from agents.json (dashboard_chat defaults true — see
        // handle_agents in bin/agent-server.py). Chat picker hygiene: not
        // every configured agent (e.g. a low-capability relay) is meant to
        // be chatted with directly from the dashboard.
        label: cfg.label || cfg.name,
        dashboard_chat: cfg.dashboard_chat !== false,
        // Everything below has a real source in the two responses above.
        // The shape used to carry messages_processed, session_age_seconds,
        // token_usage, cost, subprocess_pid, queue_depths and
        // compaction_count as well -- fields the dead /status was imagined
        // to return. They were shipped hardcoded to 0/undefined/{} and the
        // agents page rendered them, so "0 messages processed" was not a
        // reading, it was a literal. Nothing serves them, so nothing claims
        // them. Cost is on /api/cost; per-agent queue rows are on
        // /api/agents/{name}/queue.
        subprocess_alive: info.alive,
        total_pending: info.queue_depth || 0,
        session_id: info.session_id,
      };
    });

    return NextResponse.json({ agents });
  } catch (error) {
    return NextResponse.json(
      { error: "Failed to fetch agents" },
      { status: 500 }
    );
  }
}
