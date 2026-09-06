# PAI motion assets

Product-tour assets for the PAI public introduction at the Cloudflare Pages address [https://pai-loop.pages.dev/](https://pai-loop.pages.dev/). The revised tour was verified and deployed on 2026-09-07.

The footage uses the actual PAI application UI captured locally with synthetic demonstration data. It is not a recording of production records or a real company's score, private evidence, participation decision or award result. The introduction and tour must retain the visible example-data label.

- `pai-product-tour.webm`: 18-second silent VP9 product tour, 1280×720, 432 frames at 24 fps, 2,577,517 bytes. Metadata, WebM indexes and five decoded frames passed inspection.
- `pai-product-poster.webp`: 1280×720 static poster for the same product tour, 70,766 bytes; visually verified.

The five scenes show notice selection, AI extraction of requirements and scoring tables, source validation and company-condition comparison, separate eligibility/scoring/risk information, and the human participation decision. AI does not autonomously search, finalize scores, decide participation or learn new global rules from the recorded outcome. Uncalculated or review-required states must remain visible when present in the captured UI.

Only the two named files are allowed in the published `dist/assets/` folder. The previous `pai-loop-background.webm` and `pai-loop-poster.webp` are not used by this revision and are removed from generated output by the build. Capture scripts, source screenshots and this README are not deployment assets.

The page supplies native video controls and a static poster, and suppresses automatic playback when reduced motion is requested. Publication status and final media measurements should be updated only after verification.
