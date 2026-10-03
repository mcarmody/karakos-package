"use client";

import { useEffect, useState } from "react";
import { usePoll } from "@/lib/hooks";
import type { AgentRosterRow } from "@/lib/agentStatus";
import type { UsageBody } from "@/lib/packageBackend";
import AccountStrip from "./AccountStrip";
import HiveCallLog from "./HiveCallLog";
import ShardTable from "./ShardTable";

/** Epoch seconds, refreshed every 15 s (the poll interval). Seeded lazily so
 * a server render never bakes a time into markup. */
export function useNowSec(): number {
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => {
    const id = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 15000);
    return () => clearInterval(id);
  }, []);
  return now;
}

/** `/fleet` under the package profile: account strip, shard table, hive log. */
export default function PackageFleet() {
  const { data: agentsData, error: agentsError } = usePoll<{ agents: AgentRosterRow[] }>("/api/agents", 15000);
  const { data: usage } = usePoll<UsageBody>("/api/usage", 15000);
  const now = useNowSec();
  const agents = agentsData?.agents ?? [];
  const shardIds = [...new Set(agents.flatMap((a) => (a.shards ?? []).map((s) => s.id)))];

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-4" style={{ color: "var(--text-primary)" }}>
        Fleet
      </h1>
      <AccountStrip usage={usage} now={now} />
      {agentsError && !agentsData && (
        <p className="text-sm mb-4" style={{ color: "var(--err)" }}>Agent roster unavailable: {agentsError}</p>
      )}
      <ShardTable
        now={now}
        agents={agents.map((a) => ({ name: a.name, label: a.label, shards: a.shards ?? [] }))}
      />
      <HiveCallLog shards={shardIds} />
    </div>
  );
}
