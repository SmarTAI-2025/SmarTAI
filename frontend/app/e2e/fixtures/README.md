# Local PDF preview acceptance

These synthetic PDFs contain no student or production data. `single`, `multiple`,
`long`, and `mixed` have 1, 15, 240, and 20 pages respectively. `mixed` alternates
portrait and landscape pages; `broken` is intentionally invalid. Regenerate with
the repository's locked PyMuPDF 1.28.2: `python e2e/fixtures/generate.py`.

From `frontend/app`, run:

```sh
npm ci
npx playwright install chromium
npm run e2e -- e2e/pdf-preview.spec.ts --headed
```

The fixture page mounts the real `OriginalFilePreviewPanel` and comparison
workspace. The final test also opens the real question-review route with local
GET responses, a synthetic login identity, and all non-GET backend requests
blocked. No running backend, model keys, or paid calls are needed. An existing
local Vite server can be selected with `SMARTAI_E2E_FRONTEND_URL` (start it on
that port first). The harness is not an application route or production entry.

The suite uses wheel input to traverse **every page from 1 to 15**, scrolls back,
checks the synchronized counter, retains question selection, tests jumps/arrows
and invalid input, and exercises 240 pages, rapid file changes, delayed results,
unmount/worker termination, splitter resizing, CSS zoom, mobile/outer scrolling,
and loading failure. Screenshots are emitted to Playwright's output directory.
It asserts at most five canvases / 16 million total pixels in the tested desktop
viewport; the implementation's general bound is visible pages plus one page on
each side, with at most 4 million pixels per canvas and DPR capped at 2.

Chinese built-in `china-s` deliberately exercises PDF.js CMaps/font fallback;
math uses embedded fallback fonts, and text also uses standard Helvetica/Times.
The CMap fixture can emit the known Heiti-system-font and missing-glyf recovery
warnings. The real-route test records these exact warnings as an attachment and
rejects other warnings/errors. Inspect the screenshots for Chinese and math
glyphs; a passing build alone does not establish visual correctness.

`initialPage` remains an optional, one-based navigation request: a changed value
jumps to that page, while manual scrolling only updates the viewer's counter.
The viewer does not change selected questions or persist outer task state.
Safari's explicit canvas-context path and native viewer fallback remain covered
by unit tests; this suite's graphical acceptance is Chromium, not real Safari.
