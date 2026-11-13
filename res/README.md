# Janus-Ops — Brand & Design Resources

Single source for the Janus-Ops visual identity and the **Obsidian Graphite NOC** design system.

| Path | Content |
|---|---|
| `Janus-Ops-logo.png` | Master logo, 2048×2048, transparent background |
| `logos/janus-ops-logo-{256,512}.png` | Logo cropped to its content, transparent — sidebar, navbar, docs |
| `logos/janus-ops-app-icon-512.png` | Rounded app icon on `#090A0F` with `#10B981` border |
| `favicons/favicon.ico` | Multi-size ICO (16, 32, 48, 64) |
| `favicons/favicon-{16x16,32x32}.png` | Transparent PNG favicons (larger sizes: `favicon.ico`, up to 64) |
| `favicons/apple-touch-icon.png` | 180×180, opaque on `#090A0F` |
| `favicons/android-chrome-{192,512}.png` | PWA icons, opaque on `#090A0F` |
| `favicons/site.webmanifest` | PWA manifest (icon paths assume the files are served from the web root) |
| `design-tokens.json` | Design tokens (colors, semantic states) |
| `globals.css` | Base CSS for Tailwind v4 (variables, scrollbar, metric font) |
| `obsidian-graphite-palette-sheet.png` | Visual palette reference |
| `HexagonHealthWidget.tsx`, `NodeEnvironmentCard.tsx` | Reference React components |
| `mockups/janus-ops-noc-preview.html` | App shell preview (sidebar, brand, navigation) |

## Notes

- Brand name is written **Janus-Ops**. In the UI wordmark, "Ops" uses `--accent-primary`.
- All icons are generated from `Janus-Ops-logo.png`. Regenerate them from that file if the logo changes.
- The master logo is highly detailed: at 16×16 it is barely recognizable. A simplified mark
  for small sizes would be an improvement.
- The mockup loads Tailwind from a CDN for preview purposes only. The application ships every
  asset and font locally (air-gapped support).
