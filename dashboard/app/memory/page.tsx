import type { Metadata } from "next";
import { parseView } from "@/lib/memoryView";
import MemoryClient from "./MemoryClient";

export const metadata: Metadata = { title: "Memory · Karakos" };

// Always live: the graph changes with ordinary agent activity.
export const dynamic = "force-dynamic";

export default async function MemoryPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const first = (k: string) => (Array.isArray(sp[k]) ? sp[k]![0] : sp[k]) ?? null;
  return <MemoryClient initial={parseView({ get: first })} />;
}
