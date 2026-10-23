# Running a complete browser workflow inside Docker

This guide records the GOV.BR / Meu INSS investigation and the setup we validated
on 2026-10-11. Reuse the environment and diagnostic method in other projects;
validate each site's login and subsequent authenticated requests separately.

## What worked

The final image runs Python, real Google Chrome, Xvfb, and the fonts entirely
inside Docker. Playwright connects to Chrome through CDP inside the container.
The launcher supplies credentials through an environment file and mounts the
output directory. It does not use the host's browser, display, profile, or fonts.

The full Docker run on 2026-10-11 completed in **49.7 seconds**. Customer
information returned HTTP 200, and the four credit records and benefits matched
the working native baseline. That run used overlapping historical windows and
returned `200, 200, 200, 404, 404`; the two 404 responses represented windows
without records in this test account. The workflow now defaults to five
consecutive, nonoverlapping 12-month windows, ending the newest on the run date.
All 16 unit tests also passed.

These are local validation results, not a success-rate benchmark or a guarantee
that a different site or cloud IP will be accepted.

## Why solving the images was not enough

There are several separate checkpoints:

1. The challenge loads and every required phase is completed.
2. hCaptcha issues a response token.
3. The browser submits that token with the site's current form and session.
4. The site's server accepts the submission.
5. Login finishes and the application creates usable authentication tokens.
6. Authenticated customer and historical requests succeed with those tokens.

During the failing Docker runs, hCaptcha returned `pass: true`, but GOV.BR
returned HTTP 400 with “Captcha inválido.” The message alone did not identify
which server-side check failed.

Our diagnostic compared token values **in memory**, recording only equality,
submission counts, timing, and cookie names. It established that Docker:

- submitted the exact token just issued by hCaptcha;
- submitted it once, approximately 0.11 seconds after observing its issuance;
- preserved the CSRF value and main session cookies;
- still received a rejection from GOV.BR.

The successful host control showed the same session continuity. Temporary
cookies disappeared in both environments, so their disappearance did not
explain the failure. These observations narrowed the investigation beyond
image selection, missing form fields, stale tokens, or duplicated submissions
for those particular runs.

hCaptcha documents a separate server-side verification step. A frontend
success event is therefore an intermediate result, not proof that the site's
protected action will be accepted. See the
[hCaptcha verification documentation](https://docs.hcaptcha.com/#verify-the-user-response-server-side).

## The environment difference that changed the result

The original image had only four font families from Liberation. The host had
a much broader desktop font installation.

| Experiment | Result |
| --- | --- |
| Original minimal font environment | Repeated GOV.BR rejection after hCaptcha issued a token |
| Host control with a fresh profile | Full login and customer/credit requests passed |
| Container with the host's font directory mounted read-only | Two full runs passed on Docker bridge networking |
| Image with Noto, DejaVu, and emoji fonts only | Did not reproduce the successful login |
| Container additionally supplied Ubuntu, Fira, and Open Sans | Full flow passed |
| Image with those fonts installed during its build, without a host font mount | Full flow passed in 49.7 seconds |

The font environment was the strongest experimentally supported cause of the
observed difference. Fonts affect available glyphs and rendered text metrics,
so they can change browser observations used by a site or CAPTCHA provider.
**We did not observe GOV.BR's private verification response or risk rules.**
We cannot identify its exact decision rule or claim that any particular font
family is individually required. The added families were tested together.

Previous experiments changing the browser version, profile, sandbox setting,
timezone, or network mode had not resolved the rejection. The successful font
tests used bridge networking. There is no evidence from this investigation
that host networking or a particular IPv6 configuration is required.

A temporary font mount was useful for isolating a variable. The final image
installs the fonts itself so deployment does not depend on the laptop.

## The working recipe

Use the repository files as the executable reference:

- [Dockerfile](../Dockerfile): Python, Chrome, Xvfb, fonts, and application code.
- [docker_run.py](../docker_run.py): runtime flags, credentials, and output mount.
- [docker-seccomp.json](../docker-seccomp.json): Chrome sandbox support.
- [browser.py](../src/captcha/browser.py): browser and virtual display lifecycle.

| Component | Current configuration |
| --- | --- |
| Platform | Linux x86-64; the Chrome download is an amd64 package |
| Browser | Google Chrome, with a real window on container-local Xvfb |
| Display | 1280 × 900, 24-bit color; X11 selected explicitly |
| Control | Playwright attaches to Chrome's container-local CDP endpoint |
| Browser user | Non-root; launcher maps the caller's UID/GID |
| Sandbox | Bundled seccomp profile; no `--privileged` or `--no-sandbox` |
| Process lifecycle | Docker `--init`, managed Chrome/Xvfb processes, `--rm` |
| Network | Docker bridge; no published CDP port |
| Shared memory | Launcher sets `--shm-size 1g`; Chrome also currently uses `--disable-dev-shm-usage` |
| Locale | `TZ=America/Sao_Paulo`, Chrome `--lang=pt-BR`; observed navigator language remained `en-US` |
| Profile | Fresh `/tmp/chrome-profile` inside each container |
| Fonts | Liberation, Noto, DejaVu, emoji, Open Sans, Ubuntu, and Fira; the Pop!_OS font package also supplies Roboto Slab |
| Runtime inputs | `.env` via `--env-file`; never copied into the image |
| Persistent output | Only `runs/` is bind-mounted by the launcher |

The sandbox profile follows
[Playwright's Docker guidance](https://playwright.dev/python/docs/docker),
which describes the namespace permissions Chrome needs. Our launcher allocates
shared memory without sharing the host's IPC namespace. Neither the sandbox
nor the shared-memory setting was established as the CAPTCHA fix.

Ubuntu and Pop!_OS font packages are downloaded at pinned versions and verified
with SHA-256. Their URLs and checksums are in the Dockerfile. Standard Debian
font packages are installed through APT.

The complete build is **not fully version-pinned**: the base tag, current Chrome
download, APT repositories, and Python dependency resolution can change.
For a deployment, promote the same tested image rather than assuming a later
rebuild is identical. Record the image ID, browser version, and dependency
versions with a validation run; repeat validation after upgrades.

## Build and run

Use a regular non-root Linux account with Docker access. The launcher passes
that account's UID/GID into the container and expects it to own `runs/`.

Create the ignored `.env` locally from `.env.example` and set the required
credentials. Then, from the repository root:

```bash
docker build -t captcha-inss:local .
python3 docker_run.py --cpf YOUR_CPF --out runs/docker-validation \
  --model meta/muse-spark-1.3-contributor --reasoning-effort minimal
```

Host Chrome, Xvfb, and fonts are unnecessary. The host launcher uses Python's
standard library. The launcher intentionally rejects an external `--cdp-url`
or `--profile-dir` so this path owns its browser and temporary profile.

Useful environment checks:

```bash
docker run --rm --entrypoint google-chrome captcha-inss:local --version
docker run --rm --entrypoint fc-match captcha-inss:local 'Ubuntu'
docker run --rm --entrypoint fc-match captcha-inss:local 'Fira Sans'
docker run --rm --entrypoint fc-match captcha-inss:local 'Open Sans'
docker run --rm --entrypoint python captcha-inss:local \
  -m unittest discover -s tests -v
```

`fc-match` can return a fallback even when the requested family is absent;
check the returned family name. These checks verify installation, not login.

## Reusing the method in another project

1. **Capture a successful baseline.** Record browser/package versions, display
   dimensions, locale, font availability, runtime flags, and the complete
   application's results. Keep credentials and raw tokens out of reports.
2. **Build the entire runtime into the image.** A container connected to host
   Chrome proves only that the client code runs in Docker.
3. **Locate the first differing checkpoint.** Distinguish browser startup,
   challenge loading, image solving, token issuance, form submission, server
   acceptance, application token capture, and downstream API requests.
4. **Instrument narrowly.** Check token equality, CSRF continuity, submission
   count, timing, response status, failed resources, and JavaScript errors.
   Log booleans and names instead of token, cookie, or password values.
5. **Change one variable or clearly identified group at a time.** Repeat the
   successful condition. If a diagnostic needs host files, replace the mount
   with build-time dependencies before calling the image self-contained.
6. **Validate the site's actual outcome.** For GOV.BR, reaching the password
   page is insufficient. For another SSO client, its application token storage
   and post-login API flow may differ even when the login page is shared.
7. **Repeat on the target infrastructure.** A VPS changes the public IP,
   networking, resources, and potentially browser-visible hardware properties.
   A locally successful image does not guarantee acceptance there.

Use real browser capabilities and inspect what the browser actually exposes.
Our fix did not add JavaScript property spoofing, copied authentication tokens,
or a replacement CAPTCHA verification response. A configured language flag,
for example, was not proof of the language returned by `navigator.language`.

## Other issues uncovered during validation

A pale mountain tile was incorrectly classified as an unloaded placeholder.
The old readiness heuristic rejected near-white tiles with channel standard
deviation below 9; the real image measured approximately 7–8. Reducing the
uniformity threshold to 2 allowed that loaded image through while preserving
the blank-placeholder check. A regression test covers pale content versus
an empty tile. This was a separate solver bug, not evidence of token rejection.

One native run also reached password submission but failed to obtain usable
application tokens. A subsequent fresh-profile run completed all follow-up
requests. Preserve this distinction when diagnosing failures: accepted CAPTCHA,
completed login, and usable application credentials are separate outcomes.

The committed documentation contains no customer identifiers, credentials,
raw session tokens, or screenshots. Local investigation artifacts remain in
the ignored `runs/` directory and are not needed to build or run the image.
