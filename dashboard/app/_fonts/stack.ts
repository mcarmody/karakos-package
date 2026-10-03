/**
 * Google Fonts serves one family as several files, one per unicode subset
 * (latin, latin-ext, greek, math, ...), each @font-face carrying a
 * unicode-range, so the browser fetches a subset only when the page has a
 * character from it. next/font/local cannot put several files with
 * different unicode-ranges under one family, so a family whose non-latin
 * subsets matter gets one localFont() per extra subset (each with its own
 * unicode-range declaration, no preload, no fallback face), and this joins
 * them into one font-family list:
 *
 *   'lora', 'loraMath', 'loraSymbols', 'lora Fallback'
 *
 * The extras go after the main face and before its metric-matched fallback,
 * which is a full local font (Times New Roman / Arial) and would otherwise
 * claim every character the latin file lacks. Use the result as the value of
 * the font's CSS variable, e.g. style={{ "--font-sans": fontStack(...) }}.
 */
type NextFontLike = { style: { fontFamily: string } };

export function fontStack(main: NextFontLike, ...extras: NextFontLike[]): string {
  const [own, ...rest] = main.style.fontFamily.split(",").map((f) => f.trim());
  const extraFaces = extras.map((e) => e.style.fontFamily.split(",")[0].trim());
  return [own, ...extraFaces, ...rest].join(", ");
}
