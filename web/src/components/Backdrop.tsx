import { useGpuTier } from "@nous-research/ui/hooks/use-gpu-tier";

/**
 * Replicates the visual layer stack of `<Overlays dark />` from
 * `@nous-research/ui` without pulling in its leva / gsap / three peer deps.
 *
 * See `design-language/src/ui/components/overlays/index.tsx` for the source of
 * truth. Defaults match LENS_0 (the Hermes teal dark preset); the deep canvas
 * and the warm vignette both read theme-switchable CSS custom properties so
 * `ThemeProvider` can repaint the stack without remounting.
 *
 * Modified: replaced the old filler JPEG (inverted green-speckle) with a
 * clean dark gradient. The z-1 layer no longer uses mix-blend-mode: difference
 * since there's no inverted image to difference with.
 *
 *   z-1   bg = `var(--background-base)`, plain
 *   z-2   clean dark gradient (or theme asset bg), plain
 *   z-99  warm top-left vignette (`var(--warm-glow)`), opacity 0.22, lighten
 *   z-101 noise grain (SVG, ~55% opacity × `--noise-opacity-mul`,
 *         color-dodge) — gated on GPU tier
 */
export function Backdrop() {
  const gpuTier = useGpuTier();

  return (
    <>
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0 z-[1]"
        style={{
          backgroundColor: "var(--background-base)",
        }}
      />

      <div
        aria-hidden
        className="pointer-events-none fixed inset-0 z-[2]"
        style={
          {
            // Clean backdrop — replaces the old filler image.
            // When a theme provides `assets.bg`, that overrides via
            // --theme-asset-bg (no CSS function wrapping — just the raw URL).
            backgroundImage:
              "var(--theme-asset-bg, radial-gradient(ellipse 70% 60% at 40% 20%, rgba(30, 80, 100, 0.3) 0%, transparent 65%), radial-gradient(ellipse 50% 50% at 85% 85%, rgba(15, 40, 60, 0.4) 0%, transparent 55%))",
            backgroundSize: "cover",
            backgroundPosition: "center",
          } as unknown as React.CSSProperties
        }
      />

      <div
        aria-hidden
        className="pointer-events-none fixed inset-0 z-[99]"
        style={{
          background:
            "radial-gradient(ellipse at 0% 0%, transparent 60%, var(--warm-glow) 100%)",
          mixBlendMode: "lighten",
          opacity: 0.22,
        }}
      />

      {gpuTier > 0 && (
        <div
          aria-hidden
          className="pointer-events-none fixed inset-0 z-[101]"
          style={{
            backgroundImage:
              "url(\"data:image/svg+xml,%3Csvg viewBox='0 0 512 512' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' fill='%23eaeaea' filter='url(%23n)' opacity='0.6'/%3E%3C/svg%3E\")",
            backgroundSize: "512px 512px",
            mixBlendMode: "color-dodge",
            opacity: "calc(0.55 * var(--noise-opacity-mul, 1))",
          }}
        />
      )}
    </>
  );
}
