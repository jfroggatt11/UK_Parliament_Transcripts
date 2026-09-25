# Tortoise AI — HTML Report Design System

Reference for all styled reports produced as HTML + PDF. The `sublq_calibration_proposal.html` in `analysis/` is the canonical example.

---

## Page geometry

| Property | Value |
|---|---|
| Page width | 680 px |
| Page height | 1056 px (US Letter at 96 dpi) |
| Content side padding | 28 px |
| Inter-page top gap | 18 px (CSS `@page` margin) |
| Page 1 top margin | 0 px (`@page :first`) |

---

## Colour system

Define in `:root` — use these variables everywhere. Never hardcode hex in component HTML.

```css
:root {
  --fuchsia:    #D946EF;               /* primary accent */
  --fuchsia-lt: #FDF4FF;               /* fuchsia tint background */
  --fuchsia-bd: rgba(217,70,239,0.35); /* fuchsia tint border */
  --slate:      #334155;               /* header/footer bg, dark headings */
  --soft-grey:  #F8FAFC;               /* card/hero bg */
  --green:      #10B981;               /* positive accent */
  --green-lt:   #F0FDF9;               /* green tint background */
  --border:     #E2E8F0;               /* all rule / border colours */
  --text-pri:   #1E293B;               /* primary body text */
  --text-sec:   #64748B;               /* secondary body text */
  --text-pale:  #94A3B8;               /* table headers, chart labels */
  --on-dark:    #CBD5E1;               /* text on dark (slate) backgrounds */
}
```

---

## Typography

```css
body {
  font-family: "Inter", "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 11px;
  line-height: 1.7;
  color: var(--text-pri);
}
```

| Role | Size | Weight | Other |
|---|---|---|---|
| Hero headline | 17 px | 600 | `color: var(--slate)`, `line-height: 1.3` |
| Hero sub | 11 px | 400 | `color: var(--text-sec)` |
| Section label | 9.5 px | 600 | uppercase, `letter-spacing: 0.8px`, `color: var(--fuchsia)` |
| Body copy | 11 px | 400 | `color: var(--text-sec)`, `margin-bottom: 12px` |
| Card label | 10 px | 600 | `color: var(--text-pri)` |
| Card desc | 10 px | 400 | `color: var(--text-sec)`, `line-height: 1.6` |
| Table header | 9 px | 600 | uppercase, `color: var(--text-pale)` |
| Table cell | 10 px | 400 | `color: var(--text-pri)` |
| Footer label | 9 px | 600 | uppercase, `letter-spacing: 0.7px`, `color: var(--fuchsia)` |
| Footer text | 9.5 px | 400 | `color: var(--on-dark)` |
| Chart title | 9.5 px | 600 | uppercase, `letter-spacing: 0.5px`, `color: var(--text-sec)` |

Do **not** use `<h1>`–`<h6>`. Use the component classes below instead.

---

## CSS foundation block

Paste this at the top of every `<style>` block, before component styles:

```css
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

/* ── CSS VARIABLES ── (paste :root block here) */

body {
  font-family: "Inter", "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 11px;
  line-height: 1.7;
  color: var(--text-pri);
  background: #EEF2F7;
  display: flex;
  justify-content: center;
  padding: 32px 0 48px;
}

/* ── PAGE GEOMETRY (print) ── */
@page {
  size: 680px 1056px;
  margin: 18px 0 0;
}
@page :first {
  margin-top: 0;
}

@media print {
  html, body { margin: 0; padding: 0; }
  body { background: #fff; display: block; font-size: 10.75px; line-height: 1.6; padding: 0; }
  .doc { width: 680px; min-width: unset; max-width: unset; overflow: visible; }
  .content { padding: 0 28px 8px; }
  .section-block { break-inside: auto; page-break-inside: auto; }
  .hero { margin: 20px 0 18px; padding: 20px 20px 20px 17px; break-inside: avoid; page-break-inside: avoid; }
  .section-divider { margin: 24px 0 10px; break-after: avoid-page; page-break-after: avoid; }
  .body-copy { margin-bottom: 10px; orphans: 3; widows: 3; }
  .pull-quote, .callout, .dim-card, .chart-wrap, .chart-row, .section-keep,
  .data-table tr, .param-table tr { break-inside: avoid; page-break-inside: avoid; }
  .chart-wrap { padding: 14px; margin: 10px 0; }
  .chart-wrap canvas, .chart-row canvas { display: block; width: 100% !important; max-width: 100% !important; }
  .chart-row { gap: 8px; margin: 10px 0; }
  .chart-title { margin-bottom: 10px; }
  .dim-grid { gap: 8px; margin: 12px 0; }
  .param-table, .data-table { margin: 12px 0; }
  .page-top-section { margin-top: 0 !important; }
  .closing-page {
    display: flex; flex-direction: column;
    break-before: page; page-break-before: always;
    min-height: calc(1056px - 18px);    /* fill the page minus inter-page gap */
  }
  .closing-content { padding: 18px 28px 28px; }
  .closing-page .section-divider { margin-top: 0; }
  .closing-page .footer { margin-top: auto; }
  .data-table thead, .param-table thead { display: table-header-group; }
}

.doc {
  width: 680px;
  min-width: 680px;
  max-width: 680px;
  background: #fff;
}
```

---

## Components

### Header

Always the first child of `.doc`. Dark Slate band, 52 px tall.

```html
<header class="header">
  <div class="header-logo">Tortoise<span>AI</span> · PRODUCT</div>
  <div class="header-pill">DOCUMENT TYPE</div>
</header>
```

```css
.header {
  background: var(--slate);
  padding: 0 28px;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header-logo { font-size: 13px; font-weight: 600; color: #fff; letter-spacing: 0.2px; }
.header-logo span { color: var(--fuchsia); }
.header-pill {
  background: var(--fuchsia); color: #fff;
  font-size: 9px; font-weight: 600; letter-spacing: 0.6px; text-transform: uppercase;
  padding: 3px 9px; border-radius: 20px; white-space: nowrap; flex-shrink: 0;
}
```

---

### Hero band

First content element. Soft-grey background with left fuchsia border.

```html
<div class="hero">
  <div class="hero-headline">Report Title</div>
  <div class="hero-sub">Subtitle or summary sentence — Month Year</div>
</div>
```

```css
.hero {
  background: var(--soft-grey);
  border-left: 3px solid var(--fuchsia);
  padding: 24px 24px 24px 21px;
  margin: 28px 0 24px;
}
.hero-headline { font-size: 17px; font-weight: 600; color: var(--slate); line-height: 1.3; margin-bottom: 6px; }
.hero-sub { font-size: 11px; color: var(--text-sec); line-height: 1.6; }
```

---

### Section divider

Fuchsia label + horizontal rule. Use as heading for every section.

```html
<div class="section-divider">
  <div class="section-divider-label">Section Title</div>
  <div class="section-divider-rule"></div>
</div>
```

```css
.section-divider { display: flex; align-items: center; gap: 10px; margin: 28px 0 14px; }
.section-divider-label {
  font-size: 9.5px; font-weight: 600; letter-spacing: 0.8px; text-transform: uppercase;
  color: var(--fuchsia); white-space: nowrap; flex-shrink: 0;
}
.section-divider-rule { flex: 1; height: 1px; background: var(--border); }
```

---

### Body copy

```html
<p class="body-copy">Normal paragraph text. <strong>Bold for emphasis.</strong></p>
```

```css
.body-copy { font-size: 11px; line-height: 1.7; color: var(--text-sec); margin-bottom: 12px; }
.body-copy strong { color: var(--text-pri); font-weight: 600; }
```

---

### Pull quote

Green-tinted. Use for a single key takeaway per section.

```html
<div class="pull-quote">One sentence takeaway that stands alone.</div>
```

```css
.pull-quote {
  background: var(--green-lt);
  border-left: 3px solid var(--green);
  padding: 12px 16px 12px 13px;
  margin: 16px 0;
  font-size: 11px; font-weight: 600; color: var(--text-pri); line-height: 1.6;
}
```

---

### Callout

Fuchsia-tinted. Use for findings, caveats, or key results.

```html
<div class="callout">
  <strong>Label:</strong> supporting explanation text.
</div>
```

```css
.callout {
  background: var(--fuchsia-lt);
  border: 0.5px solid var(--fuchsia-bd);
  border-left: 3px solid var(--fuchsia);
  padding: 12px 14px 12px 13px;
  margin: 14px 0;
  font-size: 11px; color: var(--text-sec); line-height: 1.7;
}
.callout strong { color: var(--text-pri); font-weight: 600; }
```

---

### Dimension card grid

2-column grid of summary cards. Each card has a coloured dot, label, optional metadata row, and description.

```html
<div class="dim-grid">
  <div class="dim-card">
    <div class="dim-card-header">
      <div class="dim-dot" style="background: var(--green)"></div>
      <div class="dim-card-label">Card Title</div>
    </div>
    <div class="dim-card-meta">
      <div class="dim-meta-item">KEY <span>value</span></div>
    </div>
    <div class="dim-card-desc">Short description sentence.</div>
  </div>
  <!-- repeat -->
</div>
```

```css
.dim-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 14px 0; }
.dim-card { background: var(--soft-grey); border: 0.5px solid var(--border); border-radius: 6px; padding: 14px; }
.dim-card-header { display: flex; align-items: center; gap: 7px; margin-bottom: 6px; }
.dim-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.dim-card-label { font-size: 10px; font-weight: 600; color: var(--text-pri); }
.dim-card-meta { display: flex; gap: 16px; margin-bottom: 5px; }
.dim-meta-item { font-size: 9.5px; letter-spacing: 0.4px; color: var(--text-pale); }
.dim-meta-item span { color: var(--text-pri); font-weight: 600; }
.dim-card-desc { font-size: 10px; color: var(--text-sec); line-height: 1.6; }
```

---

### Chart container

Soft-grey card wrapping a Chart.js canvas. Single column or side-by-side via `.chart-row`.

```html
<!-- Single full-width chart -->
<div class="chart-wrap">
  <div class="chart-title">Chart Title</div>
  <canvas id="myChart" height="250"></canvas>
</div>

<!-- Two side-by-side charts -->
<div class="chart-row">
  <div class="chart-wrap">
    <div class="chart-title">Left</div>
    <canvas id="leftChart" height="160"></canvas>
  </div>
  <div class="chart-wrap">
    <div class="chart-title">Right</div>
    <canvas id="rightChart" height="160"></canvas>
  </div>
</div>
```

```css
.chart-wrap {
  background: var(--soft-grey); border: 0.5px solid var(--border); border-radius: 6px;
  padding: 16px; margin: 14px 0;
}
.chart-title {
  font-size: 9.5px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase;
  color: var(--text-sec); margin-bottom: 12px;
}
.chart-row { display: flex; gap: 10px; margin: 14px 0; }
.chart-row .chart-wrap { flex: 1; min-width: 0; margin: 0; }
.chart-wrap canvas { display: block; width: 100% !important; max-width: 100% !important; }
```

Chart.js defaults to set at top of script:

```js
Chart.defaults.font.family = "'Inter', sans-serif";
Chart.defaults.font.size = 10;
```

Recommended axis/legend colours: ticks `#94A3B8`, grid lines `rgba(0,0,0,0.05)`, legend text `#64748B`.

---

### Data table

```html
<table class="data-table">
  <thead>
    <tr><th>Column</th><th>Column</th></tr>
  </thead>
  <tbody>
    <tr><td>value</td><td>value</td></tr>
  </tbody>
</table>
```

```css
.data-table { width: 100%; border-collapse: collapse; margin: 14px 0; font-size: 10px; }
.data-table th {
  font-size: 9px; font-weight: 600; letter-spacing: 0.5px; text-transform: uppercase;
  color: var(--text-pale); text-align: left; padding: 0 8px 7px; border-bottom: 1px solid var(--border);
}
.data-table td { padding: 6px 8px; border-bottom: 0.5px solid var(--border); color: var(--text-pri); vertical-align: middle; }
.data-table tr:last-child td { border-bottom: none; }
.data-table tr:hover td { background: var(--soft-grey); }
```

Grade badge helper:

```css
.grade-badge {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 4px; font-size: 9px; font-weight: 600;
}
.g-A { background: rgba(16,185,129,0.15); color: #059669; }
.g-B { background: rgba(16,185,129,0.10); color: #10B981; }
.g-C { background: rgba(245,158,11,0.15); color: #D97706; }
.g-D { background: rgba(249,115,22,0.15); color: #EA580C; }
.g-F { background: rgba(239,68,68,0.15);  color: #DC2626; }
```

---

### Footer (closing page only)

Dark Slate band. Lives inside `.closing-page` so it is pushed to the bottom of the last PDF page via `margin-top: auto`.

```html
<footer class="footer">
  <div class="footer-left">
    <div class="footer-label">Tortoise AI — PRODUCT</div>
    <div class="footer-text">Data source description.</div>
  </div>
  <div class="footer-right">
    <div class="footer-label">Generated</div>
    <div class="footer-text">Month Year<br>Version · method</div>
  </div>
</footer>
```

```css
.footer { background: var(--slate); padding: 20px 28px; display: flex; gap: 24px; align-items: flex-start; }
.footer-left { flex: 1; min-width: 0; }
.footer-right { flex: 0 0 200px; }
.footer-label { font-size: 9px; font-weight: 600; letter-spacing: 0.7px; text-transform: uppercase; color: var(--fuchsia); margin-bottom: 6px; }
.footer-text { font-size: 9.5px; line-height: 1.7; color: var(--on-dark); }
.footer-text a { color: var(--fuchsia); text-decoration: none; }
```

---

## Section block classes

| Class | Purpose |
|---|---|
| `.section-block` | Wraps a section (divider + content). Default: no forced break. |
| `.section-keep` | Adds `break-inside: avoid` — use for short sections that should not split. |
| `.page-top-section` | Zeroes `margin-top` for an element that appears near the top of a new page. |
| `.closing-page` | Forces a page break and fills the remaining page height; contains footer. |
| `.closing-content` | Padded content area inside `.closing-page`. |

---

## What not to do

- No `float` anywhere.
- No `position: fixed` or `position: absolute`.
- No `overflow: hidden` on `.doc` — this interferes with PDF pagination.
- Do not use `display: none` on the Google Fonts link at print time.
- Do not use Playwright's `footerTemplate` — it strips background colours.
- Do not set `display: flex` on `<body>` in `@media print` — use `display: block`.
