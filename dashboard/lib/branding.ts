/**
 * Dashboard branding. Neutral text: "Karakos", plus an operator-set name when
 * KARAKOS_NAME is set (inlined as NEXT_PUBLIC_KARAKOS_NAME by next.config.ts),
 * otherwise nothing.
 */

/** Operator-set display name; "" when unset. The literal `process.env.X`
 * access is what Next inlines into client bundles. */
export function operatorName(): string {
  return (process.env.NEXT_PUBLIC_KARAKOS_NAME || "").trim();
}

/** Sidebar subtitle under "Karakos"; "" means render nothing. */
export function navSubtitle(operator: string = operatorName()): string {
  return operator;
}

export interface Masthead {
  /** Accent-coloured lead word and the plain remainder of the title. */
  lead: string;
  rest: string;
  /** Small line next to the title; "" means render nothing. */
  subtitle: string;
}

export function masthead(operator: string = operatorName()): Masthead {
  return { lead: "KARAKOS", rest: " OPS BOARD", subtitle: operator ? `${operator} — All Systems` : "All Systems" };
}

/** Labels for the host resource bars on the home page. */
export function hostLabels(): { cpu: string; memory: string; disk: string } {
  return { cpu: "CPU", memory: "Memory", disk: "Disk" };
}
