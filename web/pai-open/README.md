# PAI public introduction

Static Korean introduction for PAI. It explains discovery, daily review, document evidence, eligibility and scoring, human decisions, award/performance reference, and outcome records. Interactive examples are synthetic. The page makes no application API requests or data writes.

## Local preview and build

```sh
node preview.mjs
node --check app.js
node build.mjs
```

Preview: `http://127.0.0.1:8788/`. Only the allowlisted files in `dist/` are deployment assets. Upload the contents of `dist/` through Cloudflare Workers & Pages → Create application → Upload your static files. Use the application name `pai-loop`. This static site is independent of the Render application deployment.

The current application links use `https://pai-loop-demo.onrender.com`. When the application URL changes, update those links in `index.html` together and rebuild. `data-app-path` records the intended destination route. Renaming the public site does not rename the Render application.

## Design and interaction

- Paperlogy; body tracking `0.012em`, heading tracking `0.008em`. The font CSS comes from the same CDN already used by the app.
- Keyboard-accessible preview tabs, mobile menu and FAQ; explicit synthetic example labels.
- 8-second, silent, 1280 × 720 VP9 WebM background and WebP poster, generated locally from original abstract canvas graphics. The video is 692,578 bytes. Browser recording requested 24 fps and stored 191 frames over 8 seconds. No real screens or data are used.
- Reduced-motion preferences suppress the video and animated decoration. A pause button is available during normal playback; playback failure falls back to the static presentation.
- The public page contains no organization chart, account credentials, private evidence or source documents.

## Validation

Build checks duplicate IDs, internal anchors, ARIA references and referenced media. Local browser checks covered desktop rendering, 320/390/768px embedded viewports without horizontal overflow, mobile menu keyboard/touch use, preview panels and reduced-motion fallback. The verification harness is local-only and excluded from deployment and source control.

Application login, department account permissions, pre-analysis human decision persistence and production outcome records require a separate backend contract. The public page does not implement or imply that those pending changes are available.
