# Website

Static HTML, CSS and JavaScript, with no build step. Serve this directory as the
webroot. The homepage uses `landing.css`, `landing.js`, `gate.js` and the shared
`interface.js`; other routes retain their existing layout and URLs.

The homepage uses an original Higgsfield guardian film, a restrained dark theme,
real Computer captures, and an explicitly labelled local chat demonstration.
The film pauses when off-screen or the page is hidden. Reduced-motion and Save-Data
users receive the poster by default, with an explicit play control available.
Fonts are self-hosted; their license and pinned upstream source are under `fonts/`.
There is no analytics, model request or agent connection in these interactions.

`media/computer-headless.webp` and `media/computer-desktop.webp` are actual captures
from synthetic test projects. They contain no operator identity, private access URL
or credentials. Media provenance is documented in `media/README.md`.
The Computer section is labelled experimental, matching its source feature status.

`gate.js` re-implements the shell policy for illustration. Python remains
authoritative. Re-check browser verdicts whenever `talos/policy.py` or
`talos/command_floor.py` changes; the browser cannot prove identity or approve work.

Before publication: run `tests/test_site_claims.py`, verify all local links and
anchors, exercise the demos and mobile navigation in a real browser, inspect
desktop/mobile screenshots and confirm no unexpected console or HTTP errors.
