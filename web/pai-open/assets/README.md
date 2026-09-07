# PAI motion assets

Media for the revised PAI public introduction at [PAI on Cloudflare Pages](https://pai-loop.pages.dev/), published and verified on 2026-09-07 as deployment 10dd2c82-1ee4-4e73-aa17-edf5bfc750b9. The source PR remains unmerged to avoid triggering the separate Render application deployment.

The film combines actual PAI application captures containing synthetic demonstration data with original Three.js geometry, perspective camera moves, lighting and dimensional graphics. It includes close-ups of source evidence and the human decision controls. It is not a recording of production records or a real company's score, private evidence, participation decision or award result. Keep the visible example-data label.

| Asset | Format and verified dimensions | Size | Publication |
| --- | --- | ---: | --- |
| `pai-intro-3d.mp4` | Silent H.264, 1280×720, 24.00 seconds, 24 fps, 576 frames | 3,210,962 bytes | Required |
| `pai-intro-3d-poster.webp` | WebP poster decoded from the discovery scene, 1280×720 | 47,180 bytes | Required |
| `pai-intro-3d.webm` | Silent VP9 alternate, 1280×720, 24 seconds | 2,870,253 bytes | Local alternate only; prohibited in `dist/assets/` |

The film opens with a dimensional PAI mark, then shows discovering a notice, reading document conditions, comparing linked evidence and review information, and recording a person's decision, before the closing mark. AI does not independently finalize scores or decide participation. Uncalculated and review-required states in the source UI must not be changed into claims of successful production analysis.

The current page's four-step glass story is compiled from `story.html` by `build.mjs`. Build before previewing the complete page at `http://127.0.0.1:8791/`. The film uses native video controls and a static poster; reduced motion prevents automatic playback. The separate scroll story offers a motion toggle and a sequential fallback.

Only the MP4 and its WebP poster are permitted in the published `dist/assets/` folder. Previous `pai-loop-background.webm`, `pai-loop-poster.webp`, `pai-product-tour.webm` and `pai-product-poster.webp` are retained as source history but removed from generated deployment output. The optional new WebM, this README, local rendering dependencies, source screenshots and capture/QA inputs must not be deployed. The public page makes no application API calls and contains no private source material.

Final MP4 metadata and six decoded scenes were verified, and the poster was visually inspected. The renderer and source/media hashes are kept in the local film workspace. Local layouts from 320 to 1440px, keyboard and reduced-motion behavior, and native playback were checked. The production page serves the new 24-second MP4 and poster; see the parent README for page QA scope and remaining browser coverage.
