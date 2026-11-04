# Shared Meu INSS query

`inss-query`, its `inss-extract` alias and `get_inss_payments` use the same
`inss.query` implementation. The MCP starts a worker with per-call credentials
sent over stdin. The worker calls the validated browser-to-HTTP login and then
extracts requested data. Chrome/Xvfb close before the HTTP password POST.
The original browser-only commands remain explicitly available as
`inss-browser-legacy` and `python runner.py`.

## Select the work

The default has changed: one recent API year of payments, benefit/report
metadata from the current session, and **no PDF downloads**. An API year uses
the existing calendar-window rules, not a promise of exactly 365 days.

| MCP argument | CLI equivalent | Behavior |
| --- | --- | --- |
| `years=1` | `--years 1` | 1..5 recent API windows |
| `start_date`, `end_date` | `--start-date`, `--end-date` | Both ISO dates; split into at most five nonoverlapping API windows |
| `include_payments=False` | `--skip-payments` | No credit-history requests |
| `include_letters=True` | `--include-letters` | All concession letters in this session |
| `letter_benefit_numbers=[...]` | repeat `--letter-nb` | Only those letters; enables downloads without `include_letters` |
| `medical_report_nb="..."` | `--medical-report-nb` | Only the selected eligible medical PDF |
| `fast_interactions=True` | `--fast-interactions` | Optional visual readiness/shorter interaction margins |
| `batch_captcha=True` | `--batch-captcha` | Optional image prefetch with per-round fallback |

Both dates override `years` for window selection. Future/reversed dates, missing
date pairs, unsupported ranges and malformed NBs stop before login. Document NBs
must belong to the authenticated session; medical eligibility is checked too.
Selecting documents does not automatically omit payments: set
`include_payments=False` when only documents/metadata are needed.

With Linux Chrome/Xvfb/fonts installed:

```sh
pip install .
inss-query --env-file .env --out runs/query \
  --start-date 2026-01-01 --end-date 2026-10-31
```

The ignored `.env` contains `GOVBR_CPF`, `GOVBR_PASSWORD` and
`OPENROUTER_API_KEY`. `--credentials-stdin` instead accepts one JSON object with
`cpf` and optional `password`; do not embed real values in shell commands. A
per-call nonempty password takes precedence over the local password. Environment
values take precedence over the file, whose location can be set by `INSS_ENV_FILE`.

## Windows through Docker

Build the repository image, then run the installed entry point. In PowerShell,
resolve the local paths into `$credentialPath`, `$outputPath` and `$seccompPath`
before using this example:

```powershell
docker build -t meu-inss-query:local .
docker run --rm --init --user 1000:1000 --read-only --shm-size 1g `
  --tmpfs /tmp:rw,nosuid,nodev,size=2g,mode=1777 `
  --security-opt "seccomp=$seccompPath" `
  --mount "type=bind,src=$credentialPath,dst=/run/credentials.env,readonly" `
  --mount "type=bind,src=$outputPath,dst=/out" `
  --entrypoint inss-query meu-inss-query:local `
  --env-file /run/credentials.env --out /out
```

For MCP, use `--entrypoint inss-mcp`, `-i` and **no `-t`**. Set the nonsecret
container variables `INSS_ENV_FILE=/run/credentials.env` and
`INSS_DOCUMENTS_DIR=/out/mcp`; keep the same mounts/runtime options. No bootstrap
script is needed. A returned `/out/mcp/...` maps to the host output directory.
No Python, Xvfb or Chrome installation on the Windows host is needed.

## Partial results and diagnostics

CLI output includes private `query.json`, sanitized `receipt.json` and requested
PDFs. No credentials/tokens are serialized. The MCP returns a summary, preserves
requested PDFs in its document directory, and removes temporary raw extraction.

`complete=false` / `partial=true` means one or more requested components failed;
it does not mean no payments exist. Inspect `errors`, each window's status/error,
and each document result. HTTP 404 for a payment window is treated as an empty
available period. Timeouts, other HTTP errors and malformed JSON are recorded
without discarding successful windows. No automatic password/token retry occurs.
Authentication failure stops before extraction and returns no account data.

Exit 0 can include partial results: inspect `complete`, not only the exit code.
Nonzero exit indicates authentication, argument or unrecoverable worker failure.
To restore the previous MCP scope, explicitly request `years=5` and
`include_letters=True`.

`timings` separates login, extraction and total worker milliseconds. MCP adds
`queue_ms` and `mcp_total_ms`, including worker launch, result loading and document
persistence. These are individual-call observations, not a production benchmark.
Compare identical selectors, account/runtime and success/partial states over
multiple runs before claiming speed improvements; report distributions and errors.

## Concurrency limits

The existing lock still serializes calls **within one MCP process**. Independent
servers/containers are not coordinated by it. Each worker owns its HTTP session
and temporary profile, but that does not establish a global concurrency bound,
cross-process X display allocation or browser pooling. Run separate containers
with separate temporary displays/profiles; load/concurrency tests remain pending.
No session/token cache or cross-account browser/token reuse is introduced.

## Validation

An installed-package MCP stdio call was checked inside Docker on 2026-11-04,
using the frozen reference runtime (Chrome 156.0.8078.79), one recent payment
window, all concession letters explicitly requested, fast interactions enabled
and batching disabled. Payment extraction returned HTTP 200 and requested PDFs
survived worker cleanup. Document failures were returned as partial results
alongside the successful data. Credentials and account payloads were not logged.

That single call took 51.933 s from the MCP client: login 34.494 s, extraction
15.610 s, total worker time 50.105 s. MCP timing was 51.921 s including worker
launch and persistence; client/server initialization is outside that call timer.
This is an individual end-to-end validation with partial document results, not
a speed comparison with previous login-only samples or a production benchmark.
Default document-free queries, matched baseline runs and concurrent production
load were not measured live in this check. Offline tests cover selectors,
timeout/HTTP/malformed-JSON isolation, stdin/file credential precedence and
document persistence failure without discarding payments.
The full offline regression passed 196 tests on Windows and 193 tests plus
18 subtests on Linux/Docker, with three optional Linux tests skipped. POSIX mode
assertions remain enforced on Linux; Windows checks the same file behavior
without asserting Unix permission bits.
