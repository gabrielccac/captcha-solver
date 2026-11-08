# ARIA selection confirmation — 2026-11-08

The optimized login now reads each task's `aria-pressed` in its own iframe,
instead of treating pixel-based marker recognition as a submission prerequisite.
The model still chooses local row-major positions 1–9. Labels need not be
English or restart numbering on every round.

The controller binds the visible nine-cell grid before vision inference,
validates row-major layout, and retains task image/reference/prompt identity in
memory. It uses the existing mouse movement/click implementation, with current
element bounds. Already selected cells are skipped. Changed challenges,
detached grids, unknown states and extra selections prevent submission.

After a bounded wait, only expected cells still marked `false` receive one
additional click each. State is checked again before each retry to avoid
undoing a late selection. Exact state must stabilize before submission.
The Actions whole-login retry is disabled for the ARIA backend.

## Unchanged execution environment

- Base image: `ghcr.io/gabrielccac/captcha-solver-webkit-smoke@sha256:62e6266bad61e67eaa62eb2ca6f69493b1801d9077e2e2b117693c61c28d5571`.
- WebKit 27.2, headless persistent context, temporary profile.
- Viewport 1279×812, same fonts and seccomp profile.
- Same 1 CPU / 1400 MiB container limits and HTTP handoff.
- The Jean runner mounts the reviewed Python sources over the pinned image.
  No browser, dependency or font rebuild is required.

## Tests

160 tests passed in the pinned image with network disabled, including a real
WebKit fixture with different parent/iframe origins. The parent is unable to
read the frame document through ordinary page JavaScript; frame-scoped
automation can read the accessible state.

The browser fixture deliberately ignores the first click on cell 6. Observed
trusted input sequence is `[2,6,8,6]`, with final selected set `[2,6,8]`.
Confirmed cells are never toggled again. Other regressions cover late updates,
permanently lost clicks, extra selections, unavailable attributes, changed
challenge identity, alternate labels and invalid grid ordering.

The browser fixture requires explicit `CAPTCHA_BROWSER_TESTS=1` inside the
pinned environment. Other tests need no browser launch. Two existing Pillow
deprecation warnings remain.

## Real Actions attempts, including failures

| Run | Outcome | Login elapsed | ARIA wait per round |
|---|---|---:|---|
| [42484749326](https://github.com/gabrielccac/captcha-solver/actions/runs/42484749326) | Initial candidate rejected the second round's schema before clicking; fixed the overly strict `Challenge Image 1` through `9` labels requirement | 21.998 s, unsuccessful | 191 ms in round 1 |
| [42485019983](https://github.com/gabrielccac/captcha-solver/actions/runs/42485019983) | Final candidate confirmed both rounds and captured token, but HTTP handoff did not return the password form | 26.785 s, unsuccessful | 194 / 193 ms |
| [42485175080](https://github.com/gabrielccac/captcha-solver/actions/runs/42485175080) | Same final candidate completed login and independent authenticated API returned HTTP 200 | 32.463 s, successful | 193 / 195 ms |

In the successful run the browser was released at approximately 29.528 s.
There was exactly one HTTP password POST and zero browser password POSTs.
The captcha engine took 13.420 s. Actions creation-to-completion was 68 s;
n8n dispatch/poll/decryption/cleanup took 78.49 s end-to-end.

No recovery clicks were needed in the final candidate's real attempts. Thus
live confirmation/login are verified, while recovery itself is verified by the
controlled lost-click fixture. One successful login does not establish a
production success rate or resolve the separately observed HTTP handoff failure.

Only a login and authenticated session probe were requested by these isolated
workers. They did not fetch benefit/payment documents or write spreadsheet
cells. The temporary Actions entry point and encrypted result artifacts were
removed after each run. No account credentials, image URLs or tokens are
included in this report or in the selection metrics.
