# Tortoise AI — HTML to PDF Rendering Guide

How to render a Tortoise AI styled HTML report to PDF using Playwright. The canonical renderer is `analysis/render_sublq_calibration_pdf.py`.

---

## Key decisions

| Decision | Choice | Why |
|---|---|---|
| Page size | Defined in CSS `@page`, not in Python | `prefer_css_page_size=True` avoids unit-conversion errors |
| Margins | CSS `@page { margin }` | `@page :first` lets page 1 have zero top margin (header flush to top) |
| Render signal | `window.__renderReady = true` in HTML JS | Playwright waits for this; avoids races with Chart.js async rendering |
| Media emulation | Called *before* `goto` | Ensures Chart.js and CSS both initialise in print mode |
| Chrome channel | Try `channel="chrome"` first, fall back to bundled Chromium | System Chrome renders fonts and colours more accurately |
| Background colours | `print_background=True` | Required for dark header/footer bands |
| `footerTemplate` | **Not used** | Playwright strips background colours from header/footer templates |

---

## Renderer script template

```python
from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Error, sync_playwright


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parent
    default_html = root / "my_report.html"
    default_pdf  = root / "my_report.pdf"
    parser = argparse.ArgumentParser(description="Render HTML report to PDF.")
    parser.add_argument("html", nargs="?", default=default_html, type=Path)
    parser.add_argument("pdf",  nargs="?", default=default_pdf,  type=Path)
    return parser.parse_args()


def launch_browser(playwright):
    try:
        return playwright.chromium.launch(channel="chrome", headless=True)
    except Error:
        return playwright.chromium.launch(headless=True)


def main() -> None:
    args = parse_args()
    html_path = args.html.resolve()
    pdf_path  = args.pdf.resolve()

    if not html_path.exists():
        raise SystemExit(f"HTML file not found: {html_path}")

    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        # Viewport height = page height so chart responsive sizing matches the PDF page
        page = browser.new_page(viewport={"width": 680, "height": 1056})
        page.emulate_media(media="print")           # before goto — initialise in print mode
        page.goto(html_path.as_uri(), wait_until="networkidle", timeout=60000)
        page.wait_for_function("window.__renderReady === true", timeout=60000)
        page.wait_for_timeout(750)                  # brief settle for any final repaints
        page.pdf(
            path=str(pdf_path),
            print_background=True,
            prefer_css_page_size=True,              # use @page { size } from CSS
        )
        browser.close()


if __name__ == "__main__":
    main()
```

Usage:

```bash
python render_my_report_pdf.py                    # uses defaults
python render_my_report_pdf.py report.html out.pdf
```

---

## CSS page setup (in the HTML)

```css
@page {
  size: 680px 1056px;   /* width × height — US Letter equivalent at 96 dpi */
  margin: 18px 0 0;     /* 18 px top gap between pages; no side/bottom margin */
}

@page :first {
  margin-top: 0;        /* page 1: header bar flush to top edge */
}
```

The 18 px inter-page gap provides visual breathing room at section breaks without wasting space. Page 1 has no top margin so the dark header band starts at the very top of the PDF.

---

## Last page: footer pinned to bottom

Use the `.closing-page` pattern to force the footer to the bottom of the final page regardless of how much content precedes it.

```html
<section class="closing-page">
  <div class="closing-content">
    <!-- final section content -->
  </div>
  <footer class="footer">
    <!-- dark slate footer band -->
  </footer>
</section>
```

```css
@media print {
  .closing-page {
    display: flex;
    flex-direction: column;
    break-before: page;
    page-break-before: always;
    /* Fill the page: height - inter-page gap that precedes this page */
    min-height: calc(1056px - 18px);
  }
  .closing-content { padding: 18px 28px 28px; }
  .closing-page .footer { margin-top: auto; }  /* push to bottom */
}
```

How it works:
1. `break-before: page` starts `.closing-page` at the top of a fresh page.
2. `min-height: calc(1056px - 18px)` makes the section fill the page (the 18 px inter-page margin eats into the page height for pages 2+).
3. `margin-top: auto` on the footer pushes it to the bottom of that flex column.

---

## `window.__renderReady` signal

The renderer waits for this JS global before capturing the PDF. Always set it after all async work (charts, data fetches, DOM updates) is complete.

```js
// At the END of your script, after all charts are initialised:
window.__renderReady = true;
```

If you have async initialisation:

```js
async function init() {
  await fetchData();
  buildCharts();
  window.__renderReady = true;
}
init();
```

---

## Canvas height fix (Chart.js in Chromium print mode)

Chromium's PDF renderer can double canvas heights relative to what the browser DOM reports. Pin every chart canvas to its intended height with `!important` in `@media print`:

```css
@media print {
  /* Replace these IDs and heights with your actual chart IDs and desired heights */
  #scoreChart   { height: 250px !important; }
  #sigMuL       { height: 150px !important; }
  #sigL95       { height: 125px !important; }
  #scatterChart { height: 185px !important; }
}
```

Rules:
- Set the canvas `height` HTML attribute to the same value you want on screen (e.g. `height="250"`).
- Override in print with `!important` to prevent Chromium from inflating it.
- Emulating print *before* `goto` (as in the renderer template) helps — Chart.js initialises at the correct size and doesn't resize later.

---

## Page break behaviour

These CSS classes control how content breaks across pages:

| Class | CSS | Effect |
|---|---|---|
| `.section-block` | `break-inside: auto` | Default — content can flow across pages naturally |
| `.section-keep` | `break-inside: avoid` | Entire section stays on one page (use for short sections only) |
| `.callout`, `.pull-quote`, `.dim-card`, `.chart-wrap`, `.chart-row` | `break-inside: avoid` | Component-level — prevents awkward mid-element breaks |
| `.section-divider` | `break-after: avoid-page` | Keeps the section header with the first line of content below it |
| `.closing-page` | `break-before: page` | Starts the final page fresh |

**Caution:** `break-inside: avoid` on large elements creates whitespace gaps at the bottom of pages. Only use it on elements smaller than half a page. Wrapping entire large sections in `break-inside: avoid` will add pages.

---

## Diagnosing page count issues

If the PDF has more pages than expected:

1. **Check canvas heights in print mode.** Use the browser DevTools with print emulation, or:
   ```python
   info = await page.evaluate('''() => {
       const ids = ['chartA', 'chartB'];
       return Object.fromEntries(ids.map(id => {
           const el = document.getElementById(id);
           return [id, el ? {cssH: el.offsetHeight, attrH: el.height} : null];
       }));
   }''')
   ```
   If `cssH != attrH` or either is larger than expected, the canvas is rendering too tall.

2. **Check accumulated break-inside whitespace.** Generate a test PDF with all `break-inside` rules zeroed out. If the page count drops, the break rules are accumulating gaps. Be selective.

3. **Check @page margin stacking.** If you pass both `@page { margin }` in CSS and `margin=` in the Python `page.pdf()` call, they may stack. Use one or the other — the template above uses CSS only, with `prefer_css_page_size=True`.

---

## Dependencies

```
playwright>=1.40
```

Install Playwright browsers on first use:

```bash
playwright install chromium
# Or, if you have system Chrome:
playwright install chrome
```
