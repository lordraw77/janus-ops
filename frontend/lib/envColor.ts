// Environment badge colours: a preset name from before custom colours, or any #rrggbb. Pure, so it is tested.

/** The five names an environment could have; each is a fixed colour (primary is a real blue). */
export const LEGACY_COLORS: Record<string, string> = {
  crit: "#ef4444",
  warn: "#f59e0b",
  ok: "#10b981",
  primary: "#3b82f6",
  muted: "#64748b",
};

/** Swatches offered in the editor; any other colour comes from the picker. */
export const PRESET_COLORS: { value: string; label: string }[] = [
  { value: "#ef4444", label: "Red" },
  { value: "#f97316", label: "Orange" },
  { value: "#f59e0b", label: "Amber" },
  { value: "#10b981", label: "Green" },
  { value: "#14b8a6", label: "Teal" },
  { value: "#22d3ee", label: "Cyan" },
  { value: "#3b82f6", label: "Blue" },
  { value: "#6366f1", label: "Indigo" },
  { value: "#8b5cf6", label: "Violet" },
  { value: "#ec4899", label: "Pink" },
  { value: "#64748b", label: "Grey" },
];

const HEX = /^#[0-9a-fA-F]{6}$/;

/** The colour as #rrggbb (lower case), or null for a neutral badge. */
export function envHex(color: string | null | undefined): string | null {
  if (!color) return null;
  if (color in LEGACY_COLORS) return LEGACY_COLORS[color];
  return HEX.test(color) ? color.toLowerCase() : null;
}

/** Inline style of a badge: text and border in the colour, a faint fill; undefined when neutral. */
export function envStyle(color: string | null | undefined): { color: string; borderColor: string; backgroundColor: string } | undefined {
  const hex = envHex(color);
  return hex ? { color: hex, borderColor: `${hex}66`, backgroundColor: `${hex}1a` } : undefined;
}
