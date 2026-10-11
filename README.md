# Captcha solver and Meu INSS workflow

`src/captcha/` contains the reusable Chrome/Xvfb browser and 3×3 hCaptcha image
solver. `src/inss/` handles the GOV.BR login steps and the Meu INSS customer
information and credit requests. `runner.py` keeps the existing command.

## Setup

Install `uv`, Google Chrome (`/usr/bin/google-chrome`), and Xvfb. Copy
`.env.example` to `.env` and set `GOVBR_PASSWORD` and `OPENROUTER_API_KEY`
locally. `.env`, browser profiles, and run outputs are ignored by Git.

## Run

```bash
uv run python runner.py --cpf YOUR_CPF --out runs/customer \
  --model meta/muse-spark-1.3-contributor --reasoning-effort minimal
```

The CLI also accepts `--profile-dir`, `--cdp-port`, and `--max-rounds`.
Outputs include a receipt, JSON data, CSV exports, and challenge screenshots.
The browser profile and outputs contain sensitive session and customer data.

## Check

```bash
uv run python -m unittest discover -s tests -v
```

After changes to browser, captcha, login, or API code, run the full CLI flow
with the local credentials. A valid run completes both captcha phases when
present, submits the password, receives HTTP 200 for customer info, and checks
the credit-window statuses and returned records against the known baseline.
