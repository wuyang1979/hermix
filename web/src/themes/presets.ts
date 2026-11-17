import type { DashboardTheme, ThemeTypography, ThemeLayout } from "./types";

/**
 * Built-in dashboard themes.
 *
 * Each theme defines its own palette, typography, and layout so switching
 * themes produces visible changes beyond just color — fonts, density, and
 * corner-radius all shift to match the theme's personality.
 *
 * Theme names must stay in sync with the backend's
 * `_BUILTIN_DASHBOARD_THEMES` list in `hermes_cli/web_server.py`.
 */

// ---------------------------------------------------------------------------
// Shared typography / layout presets
// ---------------------------------------------------------------------------

/** Default system stack — neutral, safe fallback for every platform. */
const SYSTEM_SANS =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const SYSTEM_MONO =
  'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace';

const DEFAULT_TYPOGRAPHY: ThemeTypography = {
  fontSans: SYSTEM_SANS,
  fontMono: SYSTEM_MONO,
  baseSize: "15px",
  lineHeight: "1.55",
  letterSpacing: "0",
};

const DEFAULT_LAYOUT: ThemeLayout = {
  radius: "0.5rem",
  density: "comfortable",
};

// ---------------------------------------------------------------------------
// Themes
// ---------------------------------------------------------------------------

export const defaultTheme: DashboardTheme = {
  name: "default",
  label: "Hermes Teal",
  description: "Classic dark teal — the canonical Hermes look",
  palette: {
    background: { hex: "#041c1c", alpha: 1 },
    midground: { hex: "#ffe6cb", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 189, 56, 0.35)",
    noiseOpacity: 1,
  },
  typography: DEFAULT_TYPOGRAPHY,
  layout: DEFAULT_LAYOUT,
};

export const midnightTheme: DashboardTheme = {
  name: "midnight",
  label: "Midnight",
  description: "Deep blue-violet with cool accents",
  palette: {
    background: { hex: "#0a0a1f", alpha: 1 },
    midground: { hex: "#d4c8ff", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(167, 139, 250, 0.32)",
    noiseOpacity: 0.8,
  },
  typography: {
    fontSans: `"Inter", ${SYSTEM_SANS}`,
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap",
    baseSize: "14px",
    lineHeight: "1.6",
    letterSpacing: "-0.005em",
  },
  layout: {
    radius: "0.75rem",
    density: "comfortable",
  },
};

export const emberTheme: DashboardTheme = {
  name: "ember",
  label: "Ember",
  description: "Warm crimson and bronze — forge vibes",
  palette: {
    background: { hex: "#1a0a06", alpha: 1 },
    midground: { hex: "#ffd8b0", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 115, 22, 0.38)",
    noiseOpacity: 1,
  },
  typography: {
    fontSans: `"Spectral", Georgia, "Times New Roman", serif`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Spectral:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;700&display=swap",
    baseSize: "15px",
    lineHeight: "1.6",
    letterSpacing: "0",
  },
  layout: {
    radius: "0.25rem",
    density: "comfortable",
  },
  colorOverrides: {
    destructive: "#c92d0f",
    warning: "#f97316",
  },
};

export const monoTheme: DashboardTheme = {
  name: "mono",
  label: "Mono",
  description: "Clean grayscale — minimal and focused",
  palette: {
    background: { hex: "#0e0e0e", alpha: 1 },
    midground: { hex: "#eaeaea", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(255, 255, 255, 0.1)",
    noiseOpacity: 0.6,
  },
  typography: {
    fontSans: `"IBM Plex Sans", ${SYSTEM_SANS}`,
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap",
    baseSize: "13px",
    lineHeight: "1.5",
    letterSpacing: "0",
  },
  layout: {
    radius: "0",
    density: "compact",
  },
};

export const cyberpunkTheme: DashboardTheme = {
  name: "cyberpunk",
  label: "Cyberpunk",
  description: "Neon green on black — matrix terminal",
  palette: {
    background: { hex: "#040608", alpha: 1 },
    midground: { hex: "#9bffcf", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(0, 255, 136, 0.22)",
    noiseOpacity: 1.2,
  },
  typography: {
    fontSans: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontMono: `"Share Tech Mono", "JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Share+Tech+Mono&family=JetBrains+Mono:wght@400;700&display=swap",
    baseSize: "14px",
    lineHeight: "1.5",
    letterSpacing: "0.02em",
  },
  layout: {
    radius: "0",
    density: "compact",
  },
  colorOverrides: {
    success: "#00ff88",
    warning: "#ffd700",
    destructive: "#ff0055",
  },
};

export const roseTheme: DashboardTheme = {
  name: "rose",
  label: "Rosé",
  description: "Soft pink and warm ivory — easy on the eyes",
  palette: {
    background: { hex: "#1a0f15", alpha: 1 },
    midground: { hex: "#ffd4e1", alpha: 1 },
    foreground: { hex: "#ffffff", alpha: 0 },
    warmGlow: "rgba(249, 168, 212, 0.3)",
    noiseOpacity: 0.9,
  },
  typography: {
    fontSans: `"Fraunces", Georgia, serif`,
    fontMono: `"DM Mono", ${SYSTEM_MONO}`,
    fontUrl:
      "https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=DM+Mono:wght@400;500&display=swap",
    baseSize: "16px",
    lineHeight: "1.7",
    letterSpacing: "0",
  },
  layout: {
    radius: "1rem",
    density: "spacious",
  },
};

export const appleTheme: DashboardTheme = {
  name: "apple",
  label: "Apple",
  description: "Light, clean Apple-inspired design — white, minimal, San Francisco",
  palette: {
    background: { hex: "#F5F5F7", alpha: 1 },
    midground: { hex: "#1D1D1F", alpha: 1 },
    foreground: { hex: "#000000", alpha: 0.05 },
    warmGlow: "rgba(0, 122, 255, 0.08)",
    noiseOpacity: 0,
  },
  typography: {
    fontSans: `-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", ${SYSTEM_SANS}`,
    fontMono: `"SF Mono", ui-monospace, ${SYSTEM_MONO}`,
    baseSize: "14px",
    lineHeight: "1.5",
    letterSpacing: "-0.01em",
  },
  layout: {
    radius: "0.75rem",
    density: "spacious",
  },
  colorOverrides: {
    primary: "#007AFF",
    secondary: "#E8E8ED",
    success: "#30D158",
    warning: "#FF9F0A",
    destructive: "#FF453A",
    card: "#FFFFFF",
    cardForeground: "#1D1D1F",
    border: "#E5E5EA",
    input: "#E5E5EA",
    ring: "#007AFF",
    muted: "#F2F2F7",
    mutedForeground: "#8E8E93",
    accent: "#007AFF",
    accentForeground: "#FFFFFF",
  },
  customCSS: `/* ═══════════════════════════════════════════════
   Apple Design System — Light, Clean, Minimal
   ═══════════════════════════════════════════════ */

/* ── Root Colors ── */
:root {
  --background: #F5F5F7;
  --background-base: #F5F5F7;
  --midground: #1D1D1F;
  --midground-base: #1D1D1F;
}

/* ── Base Layout ── */
html, body, #root, #root > div:first-child {
  background: #F5F5F7 !important;
}
body, #root {
  color: #1D1D1F !important;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", sans-serif !important;
}

/* ── Uppercase Reset ── */
h1, h2, h3, h4, h5, h6, p, span, div, li, label, a, button, [class*="uppercase"] {
  text-transform: none !important;
}
[class*="tracking-"] {
  letter-spacing: -0.01em !important;
}

/* ── Sidebar (Mac-style thin sidebar) ── */
#app-sidebar {
  width: 220px !important;
  background: rgba(255,255,255,0.85) !important;
  backdrop-filter: blur(30px) !important;
  -webkit-backdrop-filter: blur(30px) !important;
  border-right: 1px solid rgba(0,0,0,0.05) !important;
}
#app-sidebar > div:first-child {
  height: 52px !important;
  padding: 0 16px !important;
  border-bottom: 1px solid rgba(0,0,0,0.04) !important;
}
#app-sidebar > div:first-child [class*="text-"] {
  font-size: 15px !important;
  font-weight: 700 !important;
  letter-spacing: -0.02em !important;
  line-height: 1.2 !important;
  color: #1D1D1F !important;
}
/* Navigation items */
#app-sidebar nav a {
  padding: 8px 14px !important;
  margin: 1px 6px !important;
  border-radius: 8px !important;
  font-size: 13px !important;
  font-weight: 500 !important;
  color: rgba(29,29,31,0.6) !important;
  letter-spacing: 0 !important;
  transition: all 0.15s ease !important;
}
#app-sidebar nav a:hover {
  background: rgba(0,0,0,0.04) !important;
  color: #1D1D1F !important;
}
#app-sidebar nav a[class*="active"] {
  background: rgba(0,122,255,0.08) !important;
  color: #007AFF !important;
  font-weight: 600 !important;
}
#app-sidebar nav li {
  list-style: none !important;
}

/* ── Top Navigation Bar ── */
#hx-topnav {
  background: rgba(255,255,255,0.80) !important;
  backdrop-filter: blur(30px) !important;
  -webkit-backdrop-filter: blur(30px) !important;
  border-bottom: 1px solid rgba(0,0,0,0.05) !important;
  height: 48px !important;
  padding: 0 16px !important;
}
#hx-topnav .hx-logo {
  font-size: 14px !important;
  font-weight: 700 !important;
  color: #1D1D1F !important;
}
#hx-topnav .hx-btn {
  padding: 5px 12px !important;
  font-size: 12px !important;
  font-weight: 500 !important;
  color: rgba(29,29,31,0.55) !important;
  border-radius: 6px !important;
}
#hx-topnav .hx-btn:hover {
  background: rgba(0,0,0,0.04) !important;
  color: #007AFF !important;
}

/* ── Main Content Area ── */
#root > div:first-child {
  padding-top: 48px !important;
}
#app-sidebar + div {
  padding: 24px 32px !important;
  max-width: 1100px !important;
  margin: 0 auto !important;
}

/* ── Cards (Apple-style white card) ── */
[class*="card"] {
  background: #FFFFFF !important;
  border: none !important;
  border-radius: 12px !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.04), 0 1px 2px rgba(0,0,0,0.02) !important;
  transition: box-shadow 0.2s ease !important;
}
[class*="card"]:hover {
  box-shadow: 0 4px 12px rgba(0,0,0,0.06), 0 2px 4px rgba(0,0,0,0.03) !important;
}

/* ── Page Headers ── */
[class*="header"] h2, h2[class*="font-"] {
  font-size: 24px !important;
  font-weight: 700 !important;
  letter-spacing: -0.03em !important;
  color: #1D1D1F !important;
}

/* ── Buttons (Apple pill style) ── */
button, [class*="btn"], [class*="button"] {
  border-radius: 8px !important;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif !important;
  font-weight: 500 !important;
  transition: all 0.15s ease !important;
}
button:hover, [class*="btn"]:hover {
  transform: none !important;
}
button[class*="primary"], [class*="btn-primary"] {
  background: #007AFF !important;
}
button[class*="primary"]:hover, [class*="btn-primary"]:hover {
  background: #0066D6 !important;
}

/* ── Backdrop ── */
[class*="backdrop"] {
  opacity: 0 !important;
}

/* ── Page Content Spacing ── */
[class*="gap-"] {
  gap: 16px !important;
}
[class*="p-"] {
  padding: 16px !important;
}

/* ── Tables / Lists ── */
[class*="table"], [class*="list"] {
  border-radius: 12px !important;
  overflow: hidden !important;
}

/* ── Badges / Tags ── */
[class*="badge"] {
  border-radius: 20px !important;
  padding: 2px 12px !important;
  font-size: 11px !important;
  font-weight: 500 !important;
}

/* ── Plugin Page ── */
[data-component="plugin-page"] {
  background: transparent !important;
  padding: 0 !important;
}

/* ── Input Fields (Apple-style) ── */
input, select, textarea {
  background: #FFFFFF !important;
  border: 1px solid #E5E5EA !important;
  border-radius: 10px !important;
  padding: 10px 14px !important;
  font-size: 13px !important;
  color: #1D1D1F !important;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif !important;
  transition: border-color 0.15s ease !important;
}
input:focus, select:focus, textarea:focus {
  border-color: #007AFF !important;
  box-shadow: 0 0 0 3px rgba(0,122,255,0.12) !important;
}

/* ── Selection Switcher ── */
[class*="selection-switcher"] {
  display: none !important;
}

/* ── Separators ── */
[class*="border-b"], [class*="border-t"], [class*="border-r"], [class*="border-l"] {
  border-color: rgba(0,0,0,0.04) !important;
}

/* ── Scrollbar ── */
::-webkit-scrollbar {
  width: 6px !important;
}
::-webkit-scrollbar-track {
  background: transparent !important;
}
::-webkit-scrollbar-thumb {
  background: rgba(0,0,0,0.1) !important;
  border-radius: 3px !important;
}
::-webkit-scrollbar-thumb:hover {
  background: rgba(0,0,0,0.2) !important;
}
`,
};

export const BUILTIN_THEMES: Record<string, DashboardTheme> = {
  default: defaultTheme,
  midnight: midnightTheme,
  ember: emberTheme,
  mono: monoTheme,
  cyberpunk: cyberpunkTheme,
  rose: roseTheme,
  apple: appleTheme,
};
