# Meu INSS: browser → HTTP

An additional login path; existing extraction and MCP commands are unchanged.
The browser handles bootstrap, CPF and hCaptcha. A `requests.Session` handles the
protected POST, password, OAuth callback, code exchange and API bootstrap.

```text
Chrome + Xvfb, original ManagedBrowser/CDP default context
  authorize → CPF → hCaptcha engine
                     ↓ intercept protected POST before it reaches SSO
requests.Session ← browser cookies + actual User-Agent
  exact POST body, once → server returns password form
  close Chrome/Xvfb/Playwright
  password POST → callback code + matching state
  exchange code → access_token
  info bootstrap (miToken: undefined) → issued miToken
  authenticated info request → HTTP 200 → success
```

The browser still handles CPF. This is not an independent captcha token pool.
Each login starts a fresh default Chrome profile, without incognito contexts or
viewport overrides. Chrome flags, fonts, CPF typing, captcha clicks and round
waits match the reference implementation. Startup uses X11 socket readiness and
faster CDP polling, already validated separately. Legacy commands retain their
original startup defaults.

## Run through Docker (Windows or Linux)

Copy `.env.example` to `.env`; fill `GOVBR_CPF`, `GOVBR_PASSWORD` and
`OPENROUTER_API_KEY` locally. Keep this file out of version control. Then:

```sh
docker build -t captcha-inss-http:local .
python scripts/govbr_http_docker.py
```

The launcher mounts only the credential file read-only and the output directory.
It runs as a non-root user, without exposed CDP ports, with a read-only root
filesystem and a temporary in-memory profile/images directory. Xvfb provides the
display inside Docker; no Chrome window appears on Windows. Credentials are not
passed as command arguments or Docker environment variables. The validated solver
model is `meta/muse-spark-1.3-contributor`, reasoning effort `minimal`.

`runs/auth-http/receipt.json` contains success, error code, HTTP statuses, POST
counts, browser version and phase/round timings. It excludes CPF, password, key,
tokens, cookies, request bodies, full URLs and account data. Exit code 0 means
verified API access; 3 means failure. No password/token retry or OTP submission is
performed. Consent requiring interaction is reported as incomplete login; existing
consent was used in validation.

On Linux with the Dockerfile's Chrome/Xvfb/fonts installed:

```sh
pip install .
inss-login-http --env-file .env --out runs/auth-http
```

`--fast-interactions` is opt-in on both the CLI and Docker launcher. It confirms
the 3x3 widget's teal check marks and visual stability instead of sleeping 2.5 s
per round, and uses smaller fixed click/focus margins. CPF typing cadence,
Chrome/CDP/default context and viewport remain the same. CPF submission waits for
the displayed value and enabled button. Missing selection confirmation stops with
`captcha_selection_not_confirmed`, without retrying clicks or posting a password.
The pixel detector is calibrated to the observed widget theme; a changed theme
requires revalidation. The default interaction timing remains the reference timing.
The standalone CLI with this option passed a real authorized-account check on
2026-11-03 in 32.227 s, with authenticated API HTTP 200 and zero browser password
POSTs. The separate experimental image-batching prototype is not included in
this CLI option. These checks do not establish a production success rate.

## Reuse a session in memory

See [session validity, reuse and reauthentication](session-tokens.md) for the
observed token lifetimes, validated behavior and the limits of token renewal.

```python
import requests
from inss.http_login import login

# Values come from your local credential input/provider.
with requests.Session() as session:
    receipt = login(session, cpf, password, api_key)
    # Authorization and miToken are now in session.headers. Send only to trusted
    # Meu INSS API endpoints; never log or serialize these headers.
```

The caller owns and closes the fresh session. The CLI closes it after validation
and does not export tokens. JWT `sub` decoding checks identity consistency only;
it does not verify JWT signatures. Success additionally requires API HTTP 200
using both issued tokens over verified TLS.

## Validation and limits

```sh
uv run python -m pytest tests -q
```

Auth unit tests use fake sessions/pages without SSO or model-provider access. On
2026-11-02, the packaged entry point and Docker launcher also passed a real check
with authorized local credentials: one HTTP captcha POST, one HTTP password POST,
zero browser password POSTs and authenticated API HTTP 200, in 38.816 seconds.
The validation image reused the reference runtime (Chrome 156.0.8078.79) with the
new package installed, preserving fonts and dependencies. Rebuilding the standard
Dockerfile can update Chrome; each receipt records the actual version. Live checks
require local credentials; receipts and private account data are not committed.

Scaling, browser pooling, concurrent logins, other accounts/consent states and
recovery after expiration have not been validated. A token is consumed once in
its original browser/HTTP session context. The protocol depends on SSO/API behavior.
