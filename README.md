# Captcha solver and Meu INSS workflow

`src/captcha/` contains the reusable Chrome/Xvfb browser and 3×3 hCaptcha image
solver. `src/inss/` handles the GOV.BR login steps and the Meu INSS customer
information and credit requests. `src/comprasnet/` validates the same GOV.BR
login on Comprasnet. `runner.py` keeps the existing INSS command.

## Setup

Install `uv`, Google Chrome (`/usr/bin/google-chrome`), Xvfb, and `poppler-utils`
(provides `pdftotext` for parsing concession letters). Copy
`.env.example` to `.env` and set `GOVBR_PASSWORD` and `OPENROUTER_API_KEY`
locally. `.env`, browser profiles, and run outputs are ignored by Git.

## Run

```bash
uv run python runner.py --cpf YOUR_CPF --out runs/customer \
  --model meta/muse-spark-1.3-contributor --reasoning-effort minimal
```

The CLI also accepts `--profile-dir`, `--cdp-port`, `--cdp-url`, and `--max-rounds`.
Outputs include a receipt, JSON credit and benefit data, CSV exports, and
challenge screenshots.
The browser profile and outputs contain sensitive session and customer data.
Credit history uses five consecutive, nonoverlapping 12-month windows by
default. The newest window ends on the run date; older windows cover complete
calendar months.

For Comprasnet, add `COMPRASNET_PASSWORD` to the ignored `.env`, then run:

```bash
uv run comprasnet-login --cpf YOUR_CPF --out runs/comprasnet \
  --model meta/muse-spark-1.3 --reasoning-effort minimal
```

This checks that the GOV.BR button leads through the captcha and password
stages to `https://www.comprasnet.gov.br/intro.htm`. It writes a receipt and
challenge images under `--out`; it does not retrieve account data.

## Local MCP tool

Run `uv sync`, then register the absolute path to `.venv/bin/inss-mcp` as a
**stdio** MCP server in your chat client. It exposes `get_inss_payments(cpf,
password)`. Both values come from each tool call; the password is passed to the
login subprocess for that call and is not added to `.env`, command arguments,
or the tool result. The server uses a fresh temporary browser profile and
removes the raw extraction files after producing the payment summary.

Each call also downloads the **carta de concessao** PDF for each benefit listed
in the customer's current session. CLI PDFs are saved in `--out`; MCP PDFs are
kept under `runs/mcp/inss-*/` and returned as `concession_letters[].file_path`.
These private local files remain after the call, so the desktop client can open
them. Delete the corresponding run directory when the documents are no longer
needed. Failed or unavailable letters include their HTTP status and an error;
they do not discard the payment results.

`concession_letters[].parsed` contains labelled customer/benefit fields, initial
monthly amount (an exact decimal string in BRL), dates, bank details and credit
notices. Dates use ISO format. Missing fields stay null and `parse_status` marks
partial extraction. The original text is included for review, including any
calculation tables not mapped to structured fields. Scanned documents require
OCR, which is not included. Parsing uses local Poppler, with no model call.

`token_validity` reports issuance, declared expiry and remaining seconds for
both session tokens, plus the shortest remaining lifetime. It contains no raw
tokens. This is a snapshot of JWT claims, not a guarantee of future validity:
revocation can happen earlier. No session token is retained for reuse.

The tool returns the source payment status, paid flag, bank-return flag,
payment date, validity dates, and amount. These fields should not be treated
as independent confirmation that money is currently available at the bank.
Because the password comes from chat, the chat client and its conversation
history can see it; use this only in a client whose handling of credentials
you accept. Set the client's tool timeout to at least five minutes.

## Docker

```bash
docker build -t captcha-inss:local .
python3 docker_run.py --cpf YOUR_CPF --out runs/customer \
  --model meta/muse-spark-1.3-contributor --reasoning-effort minimal
```

To validate Comprasnet inside the same image:

```bash
docker run --rm --init --user "$(id -u):$(id -g)" \
  --security-opt "seccomp=$PWD/docker-seccomp.json" --shm-size 1g \
  --env-file .env --mount "type=bind,src=$PWD/runs,dst=/app/runs" \
  --entrypoint comprasnet-login captcha-inss:local \
  --cpf YOUR_CPF --out runs/comprasnet \
  --model meta/muse-spark-1.3 --reasoning-effort minimal
```

The image includes Chrome and Xvfb. The launcher runs the entire workflow
inside Docker with its own temporary browser profile; no host browser or CDP
connection is used. Only `.env` and the `runs/` output mount come from the host.
Run the launcher with `python3`; host Chrome, Xvfb, and Python packages are unnecessary.
The bundled seccomp profile comes from Playwright's Docker configuration and
permits Chrome's namespace sandbox with a non-root user.

The image also installs Noto/DejaVu/emoji, Open Sans, Ubuntu, and Fira fonts.
The initial minimal font installation repeatedly reached a successful hCaptcha
verdict but GOV.BR rejected the submitted token. Matching the desktop font
set resolved the rejection in our tests; GOV.BR's internal rejection reason
is not exposed. Font packages downloaded from Ubuntu and Pop!_OS are pinned
and checked with SHA-256 during the build.

Validated locally with everything inside Docker on bridge networking, using
only the output bind mount: full login, customer HTTP 200, and matching credit
and benefit records. Historical windows without records still return the
expected HTTP 404 responses.

Use a Linux x86-64 Docker host and run the launcher as a regular non-root user
with Docker access. No host Chrome, Xvfb, or fonts are required. A cloud VPS
must be validated separately: packaging the browser does not guarantee that
GOV.BR accepts the VPS's public IP and environment.

For the investigation, environment recipe, and reuse checklist, see
[Running a complete browser workflow inside Docker](docs/docker-browser.md).

## Check

```bash
uv run python -m unittest discover -s tests -v
```

After changes to browser, captcha, login, or API code, run the full CLI flow
with the local credentials. A valid run completes both captcha phases when
present, submits the password, receives HTTP 200 for customer info, and checks
the credit-window statuses and returned records against the known baseline.
