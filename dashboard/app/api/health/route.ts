import { NextRequest, NextResponse } from "next/server";
import { agentFetch, isAuthenticated, unauthorizedResponse } from "@/lib/api";
import { readFileSync, statfsSync } from "fs";
import { loadavg, cpus } from "os";

// Hardware stats for the nav header. This Next
// process runs on the host itself, so /proc and /sys are the hardware —
// no proxy hop needed. Each read degrades to null rather than failing the
// whole health payload.
function hostStats() {
  let uptime_seconds: number | null = null;
  let temp_c: number | null = null;
  let load1: number | null = null;
  try {
    uptime_seconds = Math.floor(
      parseFloat(readFileSync("/proc/uptime", "utf-8").split(" ")[0])
    );
  } catch {}
  try {
    temp_c =
      Math.round(
        parseInt(
          readFileSync("/sys/class/thermal/thermal_zone0/temp", "utf-8"),
          10
        ) / 100
      ) / 10;
  } catch {}
  try {
    load1 = Math.round(loadavg()[0] * 100) / 100;
  } catch {}
  return { uptime_seconds, temp_c, load1 };
}

// Host CPU / memory / disk for the ops board's status strip. The board reads
// `host_metrics`, which the agent-server's /health does not return, so this
// process reads the numbers directly. CPU is the busy share
// since the previous call (the board polls every 15s); the first call after a
// restart falls back to 1-minute load / cores.
let lastCpu: { idle: number; total: number } | null = null;
function readCpu(): { idle: number; total: number } | null {
  try {
    const f = readFileSync("/proc/stat", "utf-8").split("\n")[0].trim().split(/\s+/).slice(1).map(Number);
    const idle = (f[3] || 0) + (f[4] || 0);
    const total = f.reduce((a, b) => a + (Number.isFinite(b) ? b : 0), 0);
    return { idle, total };
  } catch {
    return null;
  }
}
function hostMetrics() {
  let cpu_percent: number | undefined;
  let memory_percent: number | undefined;
  let disk_percent: number | undefined;
  const now = readCpu();
  if (now && lastCpu && now.total > lastCpu.total) {
    cpu_percent = Math.round((1 - (now.idle - lastCpu.idle) / (now.total - lastCpu.total)) * 1000) / 10;
  } else {
    try {
      cpu_percent = Math.round(Math.min(100, (loadavg()[0] / Math.max(1, cpus().length)) * 100) * 10) / 10;
    } catch {}
  }
  if (now) lastCpu = now;
  try {
    const mem: Record<string, number> = {};
    for (const line of readFileSync("/proc/meminfo", "utf-8").split("\n")) {
      const m = line.match(/^(\w+):\s+(\d+)/);
      if (m) mem[m[1]] = Number(m[2]);
    }
    if (mem.MemTotal) memory_percent = Math.round((1 - (mem.MemAvailable ?? 0) / mem.MemTotal) * 1000) / 10;
  } catch {}
  try {
    const fs = statfsSync("/");
    if (fs.blocks > 0) disk_percent = Math.round((1 - fs.bavail / fs.blocks) * 1000) / 10;
  } catch {}
  return { cpu_percent, memory_percent, disk_percent };
}

export async function GET(request: NextRequest) {
  if (!isAuthenticated(request.cookies.get("karakos_session")?.value || "")) {
    return unauthorizedResponse();
  }

  const host = hostStats();
  const host_metrics = { ...hostMetrics(), temp_c: host.temp_c ?? undefined };
  try {
    const response = await agentFetch("/health");
    const data = await response.json();
    return NextResponse.json({ ...data, host, host_metrics });
  } catch (error) {
    // Agent-server down is still a valid health answer — the hardware
    // stats survive so the nav header keeps its uptime line.
    return NextResponse.json({ ok: false, agent_server: "unreachable", host, host_metrics });
  }
}
