import { ERR, INFO, OK, WARN } from "@/app/components/lamplight-ui";
import type { Tone } from "@/lib/fleetView";

/** Tone name (lib/fleetView) to the Lamplight status colour. */
export const TONE_COLOR: Record<Tone, string> = {
  ok: OK,
  info: INFO,
  warn: WARN,
  err: ERR,
  muted: "var(--text-muted)",
};
