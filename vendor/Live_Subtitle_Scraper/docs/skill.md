# Skill: Tortoise AI HTML Report + PDF

Use this skill when asked to produce a styled analytical report as an HTML file and/or PDF for Tortoise AI. Read `docs/design-system.md` and `docs/pdf-rendering.md` for full detail. The condensed rules below are enough to produce a correct file from scratch.

---

## What to produce

1. A self-contained `.html` file — no external CSS, fonts loaded from Google Fonts or system stack, Chart.js from CDN if charts are needed.
2. A `render_<name>_pdf.py` script alongside it using the pattern in `docs/pdf-rendering.md`.

Run the renderer after writing the HTML to confirm the PDF looks right before reporting done.

---

## HTML skeleton

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TITLE — Tortoise AI</title>
<!-- Add Chart.js only if you have charts -->
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
/* ── paste design-system.md CSS block here ── */
</style>
</head>
<body>
<div class="doc">

  <header class="header">
    <div class="header-logo">Tortoise<span>AI</span> · PRODUCT</div>
    <div class="header-pill">DOCUMENT TYPE</div>
  </header>

  <div class="content">
    <div class="hero">
      <div class="hero-headline">Headline</div>
      <div class="hero-sub">Sub-headline — Month Year</div>
    </div>

    <!-- Repeat this pattern for each section -->
    <div class="section-block">
      <div class="section-divider">
        <div class="section-divider-label">Section Title</div>
        <div class="section-divider-rule"></div>
      </div>
      <!-- body-copy, pull-quote, callout, chart-wrap, dim-grid, data-table -->
    </div>
  </div>

  <!-- LAST PAGE: closing-page forces a page break and pins the footer to the bottom -->
  <section class="closing-page">
    <div class="closing-content">
      <!-- Final section content goes here -->
    </div>
    <footer class="footer">
      <div class="footer-left">
        <div class="footer-label">Tortoise AI — PRODUCT</div>
        <div class="footer-text">Description of corpus / data source.</div>
      </div>
      <div class="footer-right">
        <div class="footer-label">Generated</div>
        <div class="footer-text">Month Year<br>Version info</div>
      </div>
    </footer>
  </section>

</div>
<script>
// Your JS here.
// Signal to the PDF renderer that rendering is complete:
window.__renderReady = true;
</script>
</body>
</html>
```

---

## Key rules (quick reference)

**Layout**
- `.doc` is always 680 px wide. Never change this.
- Content padding: 28 px left/right inside `.content`.
- All sections go inside `<div class="content">` except the closing page.

**Last page / footer**
- Put the final section content + `<footer>` inside `<section class="closing-page">`.
- `closing-page` triggers a CSS page break and flexes to fill the remaining page, pushing the footer band to the bottom.
- Never use `footerTemplate` in the Playwright call — it strips background colours.

**Charts (Chart.js)**
- Set `responsive: true` (default) on every chart.
- Pin canvas CSS heights with `!important` in `@media print` to prevent Chromium doubling them. See `docs/pdf-rendering.md §Canvas height fix`.
- Always set `window.__renderReady = true` **after** all charts are initialised — the renderer waits for this signal.

**Colours** — use CSS vars, never hardcode hex inside component HTML:
| Token | Hex | Use |
|---|---|---|
| `--fuchsia` | `#D946EF` | Accents, section labels, pills |
| `--slate` | `#334155` | Header bg, footer bg, headings |
| `--soft-grey` | `#F8FAFC` | Card bg, hero bg |
| `--green` | `#10B981` | Pull-quote border, positive indicators |
| `--text-pri` | `#1E293B` | Body text primary |
| `--text-sec` | `#64748B` | Body text secondary |

**Typography**
- Font: Inter (Google Fonts) → SF Pro Display → system sans fallback.
- Body: 11 px / 1.7 lh. Section labels: 9.5 px / uppercase / 600 weight / 0.8 px letter-spacing.
- Never use `<h1>`–`<h6>` — use the `.hero-headline`, `.section-divider-label`, `.dim-card-label` components instead.

**Do not**
- Float elements.
- Use `position: fixed` or `position: absolute`.
- Add `overflow: hidden` to `.doc`.
- Set `display: none` on the Inter font link before printing.
