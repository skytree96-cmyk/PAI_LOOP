# PAI public introduction

Static Korean introduction for PAI's AI-assisted public procurement review workflow. The hero, eight scroll scenes and guide explain three distinct roles: AI extracts requirements, scoring-table candidates and source evidence from documents; PAI validates the extraction and compares verified rules with company facts; a human records the final participation decision. Search, deadline sorting, award lookups and result records are not presented as autonomous AI decisions. Uncalculated, estimated and confirmed scores remain distinct.

The product tour uses the actual application interface with synthetic demonstration data. It is not evidence of a real notice's analysis, company eligibility, score or award outcome. The public page makes no application API requests, analysis calls or data writes.

## Hosting and deployment

The public introduction is served by the existing PAI Cloud Run service at
`https://pai.kma.or.kr/open` (both `/open` and `/open/` work). Application links
use same-origin paths such as `/` and `/notices`; the app's login is unchanged.
No new domain, DNS record, or certificate is needed.

The eight public files are packaged into `pai_loop/open/` by the wheel build.
Docker copies `web/pai-open` before installing the wheel, so the normal main
branch Cloud Build deployment publishes the introduction together with the app.
Only GET/HEAD requests for the exact public file allowlist bypass account login.
Other files and API paths retain their existing access controls. The old
Cloudflare Pages deployment is left available during the transition.

## Local preview and validation

```sh
node preview.mjs
node --check app.js
node build.mjs
```

Preview the introduction at `http://127.0.0.1:8788/open`. To exercise application
links and authentication, run the FastAPI app and visit `/open` on its port.
`build.mjs` validates the page and creates a standalone asset bundle; Cloud Run
packages the authoritative source files directly, so no Node runtime is needed.

## Design and interaction

- Paperlogy; body tracking `0.012em`, heading tracking `0.008em`. The font CSS comes from the same CDN already used by the app.
- Keyboard-accessible mobile menu and FAQ; native video controls and explicit synthetic example labels.
- The media is an 18-second, silent VP9 WebM tour, 1280×720 at 24 fps (432 frames, 2,577,517 bytes), plus a 70,766-byte WebP poster. Five actual UI captures use synthetic examples. Captions, transitions and a final PAI wordmark replace the previous abstract background. File structure and five decoded frames passed inspection.
- The tour illustrates selecting a notice, AI document analysis, source evidence and company-condition comparison, separate eligibility/scoring/risk results, and a human decision. Displayed scores or states must not be edited into claims of successful production analysis.
- Reduced-motion preferences prevent automatic playback. Native controls allow deliberate playback and pausing; the poster provides a static fallback.
- The public page contains no organization chart, account credentials, private evidence or source documents.

## Validation

Build checks duplicate IDs, internal anchors, ARIA references and an explicit two-file media allowlist. It removes only the two known obsolete media files from `dist/assets/` and rejects unexpected output assets. The generated tour and poster must exist before the build can pass.

Before publishing this revision, verify desktop and 320/390/768px mobile rendering, horizontal overflow, keyboard navigation, native playback controls, reduced-motion behavior, the full video and poster, and the deployed page. Do not reuse validation results from the previous abstract-video layout as proof for the new tour. Local capture and verification inputs are excluded from deployment and source control.

Application login and department permissions remain governed by the main application. The introduction does not modify those backend contracts.


## Eight-scene scroll story (2026-09-29)

The recovered left-copy/right-example layout now covers discovery, document analysis, company comparison, human decisions, reference
records, results, personal Teams notifications, and workflow management. Each example uses synthetic data and
PAI workspace colors, horizontal navigation, typography, and cards. The examples
are native HTML/CSS so labels remain sharp and accessible at every size.

`story.js` progressively enhances all eight server-rendered scenes on screens
at least 1001px wide and 650px tall. Native scrolling and the eight step buttons
share one position; inactive examples and links are excluded from keyboard
focus. Reduced-motion preferences default to the sequential experience, with
an explicit motion toggle. Mobile and JavaScript-disabled pages show every scene
in order. The public asset allowlist and wheel include `story.css` and `story.js`.

Teams copy describes personal followed-notice alerts and department daily
briefings. The page sends no messages, connects no accounts, and performs no API
calls. All example amounts, counts, records and notification cards are fictional.

The September 29 editorial review updates hero terminology, workflow management,
Teams reminders/recommendations and five FAQ answers. The separate principles
section and duplicate Teams preview note are removed. Scene order and its
keyboard navigation follow the same sequence on desktop, mobile and without JS.
