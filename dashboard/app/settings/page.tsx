"use client";

import { useEffect, useState } from "react";
import { usePoll } from "@/lib/hooks";
import ThemeToggle from "@/app/components/ThemeToggle";
import PushSettings from "@/app/components/PushSettings";

interface SystemData {
  system_name?: string;
  version?: string;
  owner?: string;
  workspace?: string;
}

interface WhoAmI {
  user: string | null;
  unrestricted?: boolean;
}

export default function SettingsPage() {
  // Settings is per-user: everything below renders relative to the
  // logged-in account. Server routes enforce the actual boundaries
  // (middleware + per-route allowlists) — this just avoids rendering
  // sections whose APIs would 403 anyway.
  const [who, setWho] = useState<WhoAmI | null>(null);
  const unrestricted = !!who?.unrestricted;

  const { data: sys } = usePoll<SystemData>(unrestricted ? "/api/health" : "", 60000);

  useEffect(() => {
    let live = true;
    fetch("/api/whoami")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (live && d) setWho(d); })
      .catch(() => {});
    return () => { live = false; };
  }, []);

  return (
    <div>
      <h1 className="text-2xl font-semibold mb-6" style={{ color: "var(--text-primary)" }}>
        Settings
        {who?.user && (
          <span className="text-sm font-normal ml-3" style={{ color: "var(--text-muted)" }}>
            {who.user}
          </span>
        )}
      </h1>

      <Section title="Appearance">
        <div className="py-2">
          <p className="text-sm mb-2" style={{ color: "var(--text-secondary)" }}>Theme</p>
          <div className="w-40">
            <ThemeToggle />
          </div>
        </div>
      </Section>

      <Section title="Notifications">
        {/* /api/push/test broadcasts to every subscribed device, so the test
            button stays with the unrestricted account. */}
        <PushSettings allowTest={unrestricted} />
      </Section>

      {unrestricted && (
        <Section title="System">
          <Row label="System Name" value={sys?.system_name || "—"} />
          <Row label="Version" value={sys?.version || "—"} />
          <Row label="Owner" value={sys?.owner || "—"} />
          <Row label="Workspace" value={sys?.workspace || "—"} />
        </Section>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div
      className="rounded-lg border p-4 mb-4"
      style={{ backgroundColor: "var(--bg-surface)", borderColor: "var(--border)" }}
    >
      <h2 className="text-sm font-semibold mb-3" style={{ color: "var(--text-secondary)" }}>
        {title}
      </h2>
      {children}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div
      className="flex justify-between py-1 text-sm"
      style={{ borderBottom: "1px solid var(--border)" }}
    >
      <span style={{ color: "var(--text-secondary)" }}>{label}</span>
      <span style={{ color: "var(--text-primary)" }}>{value}</span>
    </div>
  );
}
