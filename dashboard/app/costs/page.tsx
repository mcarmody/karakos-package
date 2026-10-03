"use client";

import AgentMoneyPanel from "@/app/components/AgentMoneyPanel";
import ConversationsTable from "@/app/components/ConversationsTable";
import TokenBudgets from "@/app/components/fleet/TokenBudgets";
import { usePoll } from "@/lib/hooks";
import type { UsageBody } from "@/lib/packageBackend";

export default function CostsPage() {
  const { data: usage } = usePoll<UsageBody>("/api/usage", 15000);
  return (
    <div className="xl:max-w-none">
      <h1 className="text-2xl font-semibold mb-4" style={{ color: "var(--text-primary)" }}>
        Costs
      </h1>
      <AgentMoneyPanel />
      <TokenBudgets budgets={usage?.budgets} />
      <ConversationsTable />
    </div>
  );
}
