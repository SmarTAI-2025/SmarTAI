# Public user guide

The bilingual teacher manual and Models & API Guide are served at `/guide/` without login. The landing page links here in the current UI language. The shared brand links to `/`, and the header uses the existing `/login` and `/frontier/enter` destinations.

## Editing

- Edit `content/chapters.json` and `content/chapters.en.json` together. These are trusted, repository-authored HTML fragments; do not load them from user input.
- Screenshot captions and placement are in `content/images*.json`. Localized screenshots are in `../public/guide/assets/captures/`.
- `manual.css`, `manual.js`, and `content/ui.en.json` control the reader and localized controls.
- Run `python user-guide/build.py` from `frontend/app`, then commit the source and generated `public/guide/index.html` together. `python user-guide/build.py --check` verifies they match.
- The generated file is checked in so the normal Vite / Cloudflare build needs no Python dependency. It uses external, lazy-loaded images; it does not embed the 30 MB offline edition.

Imported from the user-supplied bilingual editable archive on 2026-10-19. Original ZIPs are unchanged. Product screenshots contain synthetic examples supplied with the manual; they are illustrations, not evidence of live provider calls.

## Validation

Run the landing page unit tests and `npm run e2e -- public-user-guide.spec.ts`. This exercises the built public site, chapter navigation, bilingual search, images, responsive header, and home/login/Demo links. No model requests are needed.
