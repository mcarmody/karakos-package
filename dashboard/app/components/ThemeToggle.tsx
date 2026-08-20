"use client";

import { useEffect, useState } from "react";
import { applyHour } from "../lib/hour";

type Theme = "dark" | "light" | "system";

/** Dark/Light/Auto toggle for the Lamplight time-of-day theme. "Dark" and
 * "Light" pin the night/day palettes; "Auto" hands control back to the
 * clock. See app/lib/hour.ts and the [data-hour] palettes in globals.css. */
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
    localStorage.setItem("karakos-theme", t);
    applyHour();
  }

  const options: { value: Theme; label: string }[] = [
    { value: "dark", label: "Dark" },
    { value: "light", label: "Light" },
    { value: "system", label: "Auto" },
  ];

  return (
    <div
      className="flex rounded overflow-hidden border text-xs"
      style={{ borderColor: "color-mix(in srgb, var(--ink) 20%, transparent)" }}
    >
      {options.map(({ value, label }) => {
        const active = mounted && theme === value;
        return (
          <button
            key={value}
            type="button"
            onClick={() => apply(value)}
            className="flex-1 py-2 px-3 transition-colors"
            style={{
              backgroundColor: active ? "var(--elevated)" : "transparent",
              color: "var(--ink)",
              opacity: active ? 1 : 0.6,
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
