# Repository Picker Design QA

## Comparison target

- Source visual truth: `/var/folders/6d/5y0lwn995dz4f_wpd7dt2t480000gn/T/TemporaryItems/NSIRD_screencaptureui_X3YBtr/Screenshot 2026-07-23 at 09.22.26.png`
- Implementation screenshot: `/Users/seanknowles/.codex/visualizations/2026/07/23/019f8e12-4a79-7f73-8736-cfb533e94cd3/repository-picker-after-normalized.png`
- Full-view comparison: `/Users/seanknowles/.codex/visualizations/2026/07/23/019f8e12-4a79-7f73-8736-cfb533e94cd3/repository-picker-comparison-normalized.png`
- Focused comparison: `/Users/seanknowles/.codex/visualizations/2026/07/23/019f8e12-4a79-7f73-8736-cfb533e94cd3/repository-picker-comparison-focus.png`
- Compact-layout evidence: `/Users/seanknowles/.codex/visualizations/2026/07/23/019f8e12-4a79-7f73-8736-cfb533e94cd3/repository-picker-compact.png`
- State: dark theme, Remote Sandbox selected, repository picker open, Agent Platform selected with `main` as its source branch.

## Viewport and normalization

- Source pixels: `2240 × 1798`. The 60 px desktop title bar was removed, then the app content was downsampled to `1493 × 1159`.
- Implementation CSS viewport: `1493 × 1199` at device scale factor `1`; browser-rendered screenshot: `1493 × 1159` after browser chrome.
- The source density was inferred as approximately `1.5×` from its 2240 px capture width and the app's 236 px sidebar target. Both comparison images are `1493 × 1159`.

## Findings

### Initial comparison

- **P1 — Competing nested layers obscured the form.** The source opened a Select menu inside the repository Popover. The menu covered the repository URL and branch fields while the lower surface remained visible through it, making the component look broken.
- **P2 — Source branches were hard to scan.** Branches were only visible in the underlying fields or chip, while the preset list prioritized long descriptions and truncated content.
- **P2 — Selection and custom-entry hierarchy were weak.** The selected project, other presets, and the custom path used nearly identical rows without a strong structural boundary.

### Fixes applied

- Replaced the nested Select with one collision-aware Popover containing a radio-style project list.
- Added always-visible monospace branch badges, repository icons, selected checks, and a distinct selected surface.
- Added a separated, labelled repository URL and optional branch form with explicit repository-default copy.
- Preserved the existing Tellimer dark-theme tokens, Geist typography, Lucide icon language, and compact composer rhythm.
- Added `radiogroup`, `radio`, `aria-checked`, labelled inputs, and visible keyboard focus treatment.

### Post-fix comparison

- No actionable P0, P1, or P2 findings remain.
- The picker is fully opaque, readable, and contained at the normalized viewport. Radix collision handling moves it above the trigger when vertical space is tighter.
- At `1024 × 768`, 16 px collision padding keeps the panel inside the viewport. Its critical choices and fields remain visible while the helper copy can scroll.
- The longer 510 px panel is intentional: it exposes all project choices and the custom checkout fields in one stable layer instead of stacking two transient surfaces.

## Required fidelity surfaces

- **Fonts and typography:** Existing app font stack and weights are preserved. Repository branches use the existing monospace language at a compact size; labels and descriptions retain clear hierarchy without unintended wrapping.
- **Spacing and layout rhythm:** The 400 px panel uses consistent 8/12/16 px spacing, aligned icons, stable row heights, and a separate form region. No internal overlap or clipped rows were observed.
- **Colors and visual tokens:** The implementation uses existing `background`, `popover`, `accent`, `border`, `ring`, `foreground`, and `muted-foreground` tokens. Selected, hover, and focus states remain consistent with the surrounding app.
- **Image and icon fidelity:** No raster imagery was needed. Existing Lucide `GitBranch`, `Plus`, and `Check` icons are used; no custom SVG or CSS-drawn assets were introduced.
- **Copy and content:** Preset names, descriptions, repository URLs, and source branches are unchanged. New copy clearly explains the sandbox repository choice and default-branch fallback.

## Interaction and browser evidence

- Preset selection updates `aria-checked`, repository URL, source branch, and the composer chip.
- Custom repository selection clears preset values and remains selected while a URL is entered.
- An empty custom branch keeps the placeholder `Use repository default` and produces a repository-only chip.
- Focused Vitest suite: 155 passed.
- Production build: passed.
- Browser console: no picker errors. One existing React warning remains in `Sidebar.tsx` for a non-boolean `inert` attribute and is outside this change.

## Comparison history

1. Source review: blocked by the P1 nested-layer overlap and P2 hierarchy/branch-scanning issues.
2. Initial compact check: the single-layer implementation fixed the source issues, but its collision box reached 15 px above a `1024 × 768` viewport.
3. Collision-padding pass: added a 16 px viewport gutter; the compact panel now begins at `y=16`, remains usable with internal overflow, and the full-size comparison remains unchanged.

final result: passed
