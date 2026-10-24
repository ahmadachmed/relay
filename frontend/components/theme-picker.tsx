"use client";

import { useEffect, useRef, useState } from "react";
import { Moon } from "lucide-react";

type Theme = "system" | "light" | "dark";
const key = "relay.ui.theme";
function readTheme(): Theme {
  try {
    const saved = localStorage.getItem(key);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* Browser storage may be disabled. */
  }
  return "system";
}

export default function ThemePicker() {
  const [theme, setTheme] = useState<Theme>("system");

  const preference = useRef<Theme>("system");

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    function apply(value: Theme) {
      preference.current = value;
      document.documentElement.dataset.theme =
        value === "system" ? (media.matches ? "dark" : "light") : value;
    }
    const saved = readTheme();
    setTheme(saved);
    apply(saved);
    const changed = () => {
      if (preference.current === "system") apply("system");
    };
    const stored = (event: StorageEvent) => {
      if (event.key === key || event.key === null) {
        const value = readTheme();
        setTheme(value);
        apply(value);
      }
    };
    media.addEventListener("change", changed);
    window.addEventListener("storage", stored);
    return () => {
      media.removeEventListener("change", changed);
      window.removeEventListener("storage", stored);
    };
  }, []);

  function change(value: Theme) {
    setTheme(value);
    preference.current = value;
    try {
      localStorage.setItem(key, value);
    } catch {
      /* Keep this page usable without storage. */
    }
    document.documentElement.dataset.theme =
      value === "system"
        ? window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light"
        : value;
  }

  return (
    <label className="theme-picker">
      <Moon size={14} aria-hidden="true" />
      <select
        aria-label="Color theme"
        value={theme}
        onChange={(event) => change(event.target.value as Theme)}
      >
        <option value="system">System</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </label>
  );
}
