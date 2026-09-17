# PAI public introduction

Static Korean introduction for PAI's AI-assisted public procurement review workflow. The hero, seven feature sections and guide explain three distinct roles: AI extracts requirements, scoring-table candidates and source evidence from documents; PAI validates the extraction and compares verified rules with company facts; a human records the final participation decision. Search, deadline sorting, award lookups and result records are not presented as autonomous AI decisions. Uncalculated, estimated and confirmed scores remain distinct.

The product tour uses the actual application interface with synthetic demonstration data. It is not evidence of a real notice's analysis, company eligibility, score or award outcome. The public page makes no application API requests, analysis calls or data writes.

## Publication status

The revised AI introduction and product tour were deployed on 2026-09-07 to [PAI on Cloudflare Pages](https://pai-loop.pages.dev/), without a personal account name in the hostname. This static site is independent of the Render application deployment.

## Local preview and build

```sh
node preview.mjs
node --check app.js
node build.mjs
```

Preview: `http://127.0.0.1:8788/`. Build after both product-tour assets have been generated. Only the allowlisted files in `dist/` are deployment assets; README files and local capture inputs are not included. Upload the contents of `dist/` to the existing Cloudflare Pages project `pai-loop` through the logged-in internal browser.

The current application links use `https://pai-yd7xtctmra-an.a.run.app`. When the application URL changes, update those links in `index.html` together and rebuild. `data-app-path` records the intended destination route. Renaming the public site does not rename the Render application.

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

Application login, department account permissions, pre-analysis human decision persistence and production outcome records require a separate backend contract. The public page does not implement or imply that those pending changes are available.
