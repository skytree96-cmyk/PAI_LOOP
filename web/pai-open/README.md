# PAI public introduction

Static Korean introduction to PAI's AI-assisted public procurement review workflow. A four-step story — 발견, 분석, 판단, 기록 — explains the product through a glass scroll presentation and a 24-second film combining actual application screens with original dimensional graphics.

AI extracts requirements, scoring-table candidates and source evidence from documents. PAI validates the extraction and compares verified rules with company facts. A person makes and records the final participation decision. Search, deadline sorting, award lookups and result records are not presented as autonomous AI decisions; uncalculated, estimated and confirmed scores remain distinct.

The film uses actual PAI captures containing synthetic demonstration data. The story's interface illustrations are explicitly labeled examples. Neither represents a real notice's analysis, company eligibility, score or award outcome. This public page makes no application API requests, analysis calls or data writes, and includes no private source documents, evidence, account credentials or organization chart.

## Publication status

Published and verified on 2026-09-07 at [PAI on Cloudflare Pages](https://pai-loop.pages.dev/), deployment 10dd2c82-1ee4-4e73-aa17-edf5bfc750b9. The production page serves the 24-second MP4 and four-step glass story. Its source PR remains unmerged so this independent static publication does not trigger the Render application deployment or interrupt its analysis queue.

## Local build and preview

Run from `web/pai-open/`:

```sh
node --check app.js
node --check story.js
node build.mjs
node preview.mjs
```

Preview: `http://127.0.0.1:8791/`. Build before starting or refreshing the preview: `preview.mjs` serves `dist/`, and `build.mjs` compiles `story.html` into the single story slot in `index.html`. Repeat the build after changing the template, story fragment, styles, scripts or media.

Deploy only the allowlisted output in `dist/` to the existing Cloudflare Pages project `pai-loop`. README files, the standalone story fragment, rendering tools, source screenshots and local QA inputs are not deployment files. The only deployed media are `assets/pai-intro-3d.mp4` and `assets/pai-intro-3d-poster.webp`; the optional WebM alternate and older media remain outside the published output.

Application links currently use `https://pai-loop-demo.onrender.com`. When that URL changes, update links in both `index.html` and `story.html`, update the build's external-host allowlist, and rebuild. `data-app-path` records each intended application route. Renaming the public site does not rename the Render application.

## Design and interaction

- Paperlogy, a cool white and ivory background, PAI blue, and dimensional glass panels.
- On sufficiently large desktop viewports, scrolling moves the four story steps through a pinned glass stage. Step buttons support clicking, arrow keys, Home and End; only the active copy, panel and action remain available to focus and assistive technology.
- The visible motion toggle switches between the scroll presentation and a sequential layout. Reduced-motion preference defaults to the sequential layout. Small or short viewports, and pages without JavaScript, retain the complete sequential story.
- A keyboard-accessible mobile menu and native FAQ disclosures support the rest of the page.
- The silent H.264 MP4 is 1280×720, 24 seconds and 24 fps, with 576 frames. It combines actual synthetic-data UI captures, evidence and decision close-ups, original 3D objects, camera movement and Korean captions. See `assets/README.md` for exact media measurements.
- The video has native controls and a static poster. Reduced motion prevents automatic playback. Playback pauses when the video leaves the viewport or the document becomes hidden; automatic resumption respects visibility and the motion preference.

## Validation

`build.mjs` checks duplicate IDs, internal anchors, ARIA targets, static references, external link hosts and the explicit two-file media allowlist. It requires both final media files, removes the four known older assets from `dist/assets/`, and rejects any unexpected output asset, including the optional new WebM. It also checks for selected private-data patterns; that check does not replace reviewing the public content.

The final film was decoded and inspected at six timestamps; FFmpeg confirmed 1280×720, 24.00 seconds, 24 fps, 576 frames and no audio. Local layouts were inspected at 320, 390, 768, 1024, 1280 and 1440px. No horizontal page overflow was found; all four device panels fit at the tested desktop sizes. Arrow keys, Home/End, reverse navigation, active-link focus, mobile menu/Escape, sequential reduced-motion mode and explicit motion opt-in were checked. The video played with native controls, loaded its poster and paused offscreen. The production URL was checked for the new asset versions, 24-second media, active judgment stage and distinct example/estimated-score labels. Testing used the available Chromium browser; Safari and physical devices were not tested.

Application login, department account permissions, pre-analysis decision persistence and production outcome records require a separate backend contract. The public page does not implement or imply that those pending changes are available.
