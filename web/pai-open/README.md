# PAI public introduction

Korean product introduction at `https://pai.kma.or.kr/open`. The hero film and
eight scroll scenes show the actual PAI application DOM and Teams preview card
with explicitly synthetic example data. AI extracts conditions and evidence;
PAI compares verified rules with company facts; a human makes the participation
decision. Eligibility, estimated scores and confirmed results remain distinct.

## Hosting

The existing Cloud Run service serves `/open`, `/open/` and the exact public
asset allowlist in `src/pai_loop/public_open.py`. Wheel force-includes package
the page, styles/scripts, favicon, film/poster and eight WebP screenshots.
Normal main-branch Cloud Build publishes them together with the application.
Same-origin application links retain the existing login. Public pages perform
no API calls, production reads, analysis calls, messages or data writes.

The public page permits iframe embedding from its own origin and the exact
judging-site origins `https://dancing-smakager-57e08c.netlify.app` and
`https://aiedu.kma.or.kr` through CSP `frame-ancestors`. Keep the static `_headers`
policy aligned with `OPEN_CSP`. The authenticated application retains its
separate Teams frame allowlist.

## Design and interaction

- The pinned desktop story combines glass panels, a metallic device exterior,
  depth and scroll-driven scene changes. The wide fold-style frame has one
  uninterrupted display: no central hinge or crease.
- Actual application screenshots replace the hand-drawn interface imitations.
  Captions and callouts live outside the device; inactive scenes cannot overlap.
- Screens at least 901px wide and 650px tall get the pinned stage. Native scroll,
  eight chapter buttons and keyboard navigation share the same scene state.
- Reduced motion keeps the glass/device composition, switching scenes without
  animated interpolation. An explicit toggle enables motion. Smaller screens
  and JavaScript-disabled pages show all eight scenes in sequence.
- The application user-guide link opens the same-origin `/open` page.
- The approved studio film uses a clean gradient background and uninterrupted
  metallic mockup frame. It covers search, evidence and scores, participation,
  Teams notifications, automatic result recording and subsequent history review.
  Result feedback illustrates accumulated evidence, not automatic rule changes.
- The 60-second silent film remains QHD (2560x1440, 30fps), with a VP9 WebM for
  the page and an H.264 MP4 master. A floating PAI wordmark opens and closes it.
  Measured DOM targets drive zoom, subtle glass highlights and visible cursors.
  Search and decision text appear progressively in the real application fields.
- Native playback controls remain available; reduced motion prevents autoplay.
  Poster and screenshot alt text provide static fallbacks. All shown records,
  people, departments, company facts and outcomes are demonstration examples.

## Local preview and validation

From this directory:

```sh
node preview.mjs
node build.mjs
```

Preview: `http://127.0.0.1:8788/open`. The build validates IDs, anchors, ARIA
references and the ten-file media allowlist, then creates `dist/`. Cloud Run
packages source files directly; no Node runtime is needed in production.

From the repository root, with Playwright and Chrome available:

```sh
node tools/test_open_story.cjs .local/open-qa
python -m pytest tests/test_public_open.py tests/test_frontend_public_contract.py
```

Browser QA covers 320/390/768/901/1024/1280/1440px, short desktop screens, all
chapters and CTA visibility, horizontal overflow, keyboard navigation, reduced
motion, the toggle and JavaScript-disabled content. CI also verifies the release
wheel's screenshot assets and the public authentication boundary.

## Rebuilding the actual-UI film

Requires Node.js, Playwright/Chrome, `@napi-rs/canvas`, Paperlogy Regular/SemiBold/
ExtraBold TTF files and FFmpeg with libx264 and libvpx-vp9. Dependencies and fonts
are supplied locally. Only the final reviewed media is published.

```sh
node tools/capture_open_tour.cjs CAPTURES FONT_DIR
node tools/render_product_film.cjs OUTPUT FONT_DIR FFMPEG_PATH CAPTURES --stills
node tools/render_product_film.cjs OUTPUT FONT_DIR FFMPEG_PATH CAPTURES
node tools/prepare_open_assets.cjs CAPTURES OUTPUT
node web/pai-open/build.mjs
```

The capture harness serves the current real frontend locally with a synthetic
session and data. It blocks external browser requests, uses the application's
existing renderers and exercises the actual input/tab controls. Capture hooks
are injected only into the local served copy. They never enter the deployed app.
No form is submitted. `render_open_tour.cjs` remains a compatibility entry point.

The renderer writes 16 chapter stills, intro/outro PNGs, a chapter manifest,
poster, WebM and QHD MP4 master. Review every chapter, decode both full files,
verify dimensions/duration/frame rate and test native playback before publishing.
`prepare_open_assets.cjs` copies the eight 1600x1000 WebP screen textures and the
film/poster. Refresh asset query versions when changing media. Intermediate
captures, local font files, encoders and QA outputs stay outside source control.
