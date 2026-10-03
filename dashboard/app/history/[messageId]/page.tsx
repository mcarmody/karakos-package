"use client";

/**
 * Stable deep-link for one history result (Task 8be6aa80) — pastable into
 * Discord. Reuses the same detail view the search page expands inline.
 */

import { use } from "react";
import Link from "next/link";
import HistoryDetailView from "../HistoryDetailView";

export default function HistoryMessagePage({
  params,
}: {
  params: Promise<{ messageId: string }>;
}) {
  const { messageId } = use(params);

  return (
    <div className="max-w-2xl xl:max-w-none">
      <div className="flex items-center gap-3.5 mb-4">
        <Link href="/history" style={{ fontSize: 13, color: "var(--accent)" }}>
          ← History
        </Link>
      </div>
      <div className="slip-near lit" style={{ padding: "18px 20px" }}>
        <HistoryDetailView messageId={messageId} />
      </div>
    </div>
  );
}
