# PAI public introduction

Static Korean introduction to PAI's AI-assisted public procurement review workflow. A four-step story — 발견, 분석, 판단, 기록 — explains the product through a glass scroll presentation and a 24-second film combining actual application screens, flat Paperlogy wordmarks and original glass graphics.

AI extracts requirements, scoring-table candidates and source evidence from documents. PAI validates the extraction and compares verified rules with company facts. A person makes and records the final participation decision. Search, deadline sorting, award lookups and result records are not presented as autonomous AI decisions; uncalculated, estimated and confirmed scores remain distinct.

The film uses actual PAI captures containing synthetic demonstration data. The story's interface illustrations are explicitly labeled examples. Neither represents a real notice's analysis, company eligibility, score or award outcome. This public page makes no application API requests, analysis calls or data writes, and includes no private source documents, evidence, account credentials or organization chart.

## Publication status

Final glass v2, asset version `20260907-glass-3`, was published on 2026-09-07 at [PAI on Cloudflare Pages](https://pai-loop.pages.dev/), deployment `e81d8d1c-ba78-4855-8357-69e2a4c493c4`. The final version was verified locally and on the production URL (1080p media, versioned assets, default-on motion, active judgment CTA, readable contrast and no console errors). This deployment supersedes hotfix `22a71585-822b-45e7-a41f-347fd12852dd`.

The first revision's CI passed; final revision CI is pending. The source PR remains unmerged. This independent static publication does not trigger the Render application deployment or interrupt its analysis queue.

## Local build and preview

Run from `web/pai-open/`:

```sh
node --check app.js
node --check story.js
node build.mjs
node preview.mjs
```

Preview: `http://127.0.0.1:8791/`. Build before starting or refreshing the preview: `preview.mjs` serves `dist/`, and `build.mjs` compiles `story.html` into the single story slot in `index.html`. Repeat the build after changing the template, story fragment, styles, scripts or media.

Deploy only the allowlisted output in `dist/` to the existing Cloudflare Pages project `pai-loop`. README files, the standalone story fragment, rendering tools, source screenshots and local QA inputs are not deployment files. The final build permits only `assets/pai-intro-glass-v2.mp4` and `assets/pai-intro-glass-v2-poster.webp` as media. Older films, posters and WebM alternates remain outside the published output.

Application links currently use `https://pai-loop-demo.onrender.com`. When that URL changes, update links in both `index.html` and `story.html`, update the build's external-host allowlist, and rebuild. `data-app-path` records each intended application route. Renaming the public site does not rename the Render application.

## Design and interaction

- Paperlogy, a cool white and ivory background, PAI blue, and dimensional glass panels. The final revision uses explicit dark text on light sections, larger page typography and larger glass callouts.
- On desktop viewports at least 901px wide and 650px high, scrolling moves the four story steps through a pinned glass stage. Visuals follow native scrolling with 135ms damping and a 54% sharp reading plateau. Step buttons support clicking, arrow keys, Home and End. Inactive copy and panels are inert; the active CTA becomes clickable and keyboard-focusable only on its sharp reading plateau.
- At the user's explicit request, story motion defaults on even when the operating system requests reduced motion. The visible `모션 끄기` control switches to the full sequential layout; `스크롤 모션 켜기` restores motion. An explicit off choice survives viewport resizing for the current page session. Small or short viewports, and pages without JavaScript, retain the complete sequential story and its usable CTAs.
- A keyboard-accessible mobile menu and native FAQ disclosures support the rest of the page.
- The silent H.264 MP4 is 1920×1080, 24 seconds and 24 fps, with 576 frames. New typography, simple glass objects, lighting and camera motion were rendered at 2560×1440 and downsampled to 1080p. Flat 2D Paperlogy wordmarks replace the earlier dimensional mark. The actual UI textures retain their original 1280×720 or 1265×712 detail; the earlier film was not upscaled. See `assets/README.md` for exact media measurements.
- The video has native controls and a static poster. Reduced motion prevents automatic playback. Playback pauses when the video leaves the viewport or the document becomes hidden; automatic resumption respects visibility and the motion preference.

## Validation

`build.mjs` checks duplicate IDs, internal anchors, ARIA targets, static references, external link hosts and the explicit two-file media allowlist. It requires both final media files, removes six known older media files from `dist/assets/`, and rejects any unexpected output asset, including WebM alternates. It also checks for selected private-data patterns; that check does not replace reviewing the public content.

The final local film QA record, `.local/film/glass-v2-qa.json`, reports 1920×1080, 24.00 seconds, 24 fps, 576 frames and no audio. QA includes decoded scenes, native-resolution close-ups, consecutive-frame crops and a 600px embedded preview. The final MP4 is 4,788,799 bytes; its 1920×1080 WebP poster is 97,094 bytes.

Final local layouts were checked at 320, 390, 1024, 1280 and 1440px. No horizontal page overflow was found, and all four device panels fit at the tested desktop sizes. Keyboard End/Home navigation and CTA availability on the sharp plateau were checked, along with the explicit motion-off choice across resizing. Small-screen sequential layouts retain all four steps and their actions. Native video controls, poster loading and offscreen pause were checked. Testing used the available Chromium browser; Safari and physical devices were not tested. These are local final-version results; final CI and production QA remain pending.

Application login, department account permissions, pre-analysis decision persistence and production outcome records require a separate backend contract. The public page does not implement or imply that those pending changes are available.
