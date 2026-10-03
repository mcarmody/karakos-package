"use client";

import { useEffect, useState } from "react";
import { applyHour } from "../lib/hour";

type Theme = "dark" | "light" | "system";

export default function ThemeToggle() {
  // Mounted guard prevents SSR/client hydration mismatch — don't render
  // active state until after first paint.
  const [mounted, setMounted] = useState(false);
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    const stored = localStorage.getItem("karakos-theme") as Theme | null;
    if (stored === "light" || stored === "dark" || stored === "system") {
      setTheme(stored);
    }
    setMounted(true);
  }, []);

  function apply(t: Theme) {
    setTheme(t);
    // Lamplight: "light"/"dark" pin the day/night palettes, "system" hands
    // control back to the clock. applyHour reads
    // this key and restamps data-hour immediately.
    localStorage.setItem("karakos-theme", t);
    applyHour();
  }

  const options: { value: Theme; label: string }[] = [
    { value: "dark", label: "Dark" },
    { value: "light", label: "Light" },
    { value: "system", label: "Auto" },
  ];

  return (
    <div className="flex rounded overflow-hidden border text-xs" style={{ borderColor: "var(--border)" }}>
      {options.map(({ value, label }) => {
        const active = mounted && theme === value;
        return (
          <button
            key={value}
            onClick={() => apply(value)}
            className="flex-1 py-3 lg:py-1 transition-colors"
            style={{
              backgroundColor: active ? "var(--bg-elevated)" : "transparent",
              color: active ? "var(--text-primary)" : "var(--text-muted)",
              border: "none",
              cursor: "pointer",
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}
