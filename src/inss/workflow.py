#!/usr/bin/env python3
"""Meu INSS workflow: GOV.BR login, hCaptcha, API extraction, CSV/JSON exports."""
import argparse
import csv
import faulthandler
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from captcha.browser import ManagedBrowser
from captcha.hcaptcha import page_state, solve_round, vis_modal
from inss.session import extract, goto_login, harvest_tokens, submit_cpf, submit_password

faulthandler.enable()
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if not (PROJECT_ROOT / "runner.py").exists():
    PROJECT_ROOT = Path.cwd()
PASSWORD_SELECTOR = 'input[type="password"]'


def load_dotenv():
    here = str(PROJECT_ROOT)
    for p in (os.path.join(here, ".env"), os.path.join(os.getcwd(), ".env")):
        if os.path.exists(p):
            for line in open(p):
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def find_page(b, substr="sso.acesso"):
    for c in b.contexts:
        for pg in c.pages:
            try:
                if substr in pg.url:
                    if substr == "meu.inss" and "meu.inss.gov.br/" not in pg.url:
                        continue
                    return pg
            except Exception:
                continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpf", required=True, help="masked or plain CPF")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="meta/muse-spark-1.3")
    ap.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high"),
                    help="OpenRouter reasoning effort; omit to keep the model default")
    ap.add_argument("--password-env", default="GOVBR_PASSWORD")
    ap.add_argument("--profile-dir", default=None)
    ap.add_argument("--cdp-port", type=int, default=9223)
    ap.add_argument("--cdp-url", help="connect to an existing Chrome CDP endpoint")
    ap.add_argument("--max-rounds", type=int, default=3)
    args = ap.parse_args()
    os.umask(0o077)
    load_dotenv()
    t_all = time.time()
    receipt = {"cpf_mask": args.cpf[:3] + ".***.***-" + args.cpf[-2:],
               "model": args.model, "reasoning_effort": args.reasoning_effort or "default",
               "stages": {}}
    password = os.environ.get(args.password_env, "")
    if not password:
        print(json.dumps({"fatal": args.password_env + " empty"}))
        return 3
    if not os.environ.get("OPENROUTER_API_KEY"):
        print(json.dumps({"fatal": "OPENROUTER_API_KEY empty"}))
        return 2
    os.makedirs(args.out, exist_ok=True)
    cpf_digits = "".join(c for c in args.cpf if c.isdigit())
    if len(cpf_digits) != 11:
        print(json.dumps({"fatal": "CPF must have 11 digits"}))
        return 3
    if "." in args.cpf:
        cpf_masked = args.cpf
    else:
        cpf_masked = f"{cpf_digits[:3]}.{cpf_digits[3:6]}.{cpf_digits[6:9]}-{cpf_digits[9:]}"

    profiles = os.path.join(PROJECT_ROOT, "profiles")
    default_prof = os.path.join(profiles, "default")
    owner = hashlib.sha256(cpf_digits.encode()).hexdigest()
    marker = os.path.join(default_prof, ".cpf-owner")
    if args.profile_dir:
        prof = args.profile_dir
    else:
        with open(marker) if os.path.exists(marker) else io.StringIO() as fh:
            prof = default_prof if fh.read().strip() == owner else os.path.join(
                profiles, owner[:16])
    receipt["profile_dir"] = None if args.cdp_url else prof
    mgr = None
    t0 = time.time()
    if args.cdp_url:
        cdp = args.cdp_url
    else:
        mgr = ManagedBrowser(profile_dir=prof, cdp_port=args.cdp_port)
        cdp = mgr.start()
    receipt["stages"]["browser_s"] = round(time.time() - t0, 1)
    log("browser ready")
    try:
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(cdp)
            try:
                ctx = b.contexts[0]
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                t0 = time.time()
                goto_login(page)
                submit_cpf(page, cpf_masked)
                receipt["stages"]["submit_s"] = round(time.time() - t0, 1)
                log("cpf submitted, waiting modal")
                solved = False
                # poll for challenge modal (replaces fixed sleep; skips dead rounds fast)
                modal_ok = False
                deadline = time.time() + 25
                while time.time() < deadline:
                    if vis_modal(page):
                        modal_ok = True
                        break
                    page.wait_for_timeout(500)
                if not modal_ok:
                    receipt["fatal"] = "challenge never appeared"
                    print(json.dumps(receipt, indent=1))
                    return 2
                rnd = 0
                modal_retry_deadline = time.monotonic() + 25
                while rnd < args.max_rounds:
                    log(f"round {rnd} start")
                    # re-resolve page: challenge may auto-submit -> navigation.
                    # if we already landed (password stage or app), stop solving.
                    page = find_page(b, "sso.acesso") or page
                    try:
                        early = page_state(page, PASSWORD_SELECTOR)
                    except Exception:
                        early = {}
                    if early.get("done"):
                        solved = True
                        receipt.setdefault("rounds", []).append(
                            {"round": rnd, "done": "already-password-stage"})
                        break
                    app = find_page(b, "meu.inss")
                    if app is not None:
                        page = app
                        solved = True
                        receipt.setdefault("rounds", []).append(
                            {"round": rnd, "done": "already-in-app"})
                        break
                    try:
                        done, info = solve_round(page, args.out, rnd, args.model,
                                                 args.reasoning_effort, PASSWORD_SELECTOR)
                    except Exception as e:
                        info = {"round": rnd, "crashed": str(e)[:200]}
                        done = False
                        log(f"round {rnd} crashed: {str(e)[:150]}")
                        # navigation during solve often MEANS success: re-check.
                        page.wait_for_timeout(3000)
                        page = find_page(b, "sso.acesso") or find_page(b, "meu.inss") or page
                        rec = page_state(page, PASSWORD_SELECTOR)
                        if rec.get("done") or (page is not None
                                              and "meu.inss" in getattr(page, "url", "")):
                            info["state_after"] = rec
                            done, solved = True, True
                            receipt.setdefault("rounds", []).append(info)
                            break
                    log(f"round {rnd} picks={info.get('model_picks')} state={info.get('state_after')}")
                    receipt.setdefault("rounds", []).append(info)
                    if done:
                        solved = True
                        break
                    if info.get("why") == "interrupted-clicks":
                        # A blind retry can toggle already-selected tiles off.
                        break
                    if (info.get("why") in ("no-modal", "shot-fail", "tiles-not-ready")
                            and time.monotonic() < modal_retry_deadline):
                        continue
                    rnd += 1
                    modal_retry_deadline = time.monotonic() + 25
                    if not info.get("next_round_ready"):
                        page.wait_for_timeout(3000)
                if not solved:
                    receipt["fatal"] = "captcha unsolved"
                    print(json.dumps(receipt, indent=1))
                    return 2
                t0 = time.time()
                log("submitting password")
                try:
                    has_pwd = page.evaluate(
                        "() => !!document.querySelector('input[type=\"password\"]')")
                except Exception:
                    has_pwd = False
                if has_pwd:
                    submit_password(page, password)
                    log("password submitted")
                else:
                    log("no password field (already past it)")
                    app = find_page(b, "meu.inss")
                    if app is not None:
                        page = app
                page.screenshot(path=os.path.join(args.out, "after-pass.png"))
                receipt["stages"]["password_s"] = round(time.time() - t0, 1)
                toks = harvest_tokens(page, cpf_digits)
                if not (toks["auth"] and toks["mi"]):
                    receipt["fatal"] = "no valid tokens after login"
                    print(json.dumps(receipt, indent=1))
                    return 3
                t0 = time.time()
                log("extracting via requests")
                data = extract(cpf_digits, toks["auth"], toks["mi"])
                receipt["stages"]["extract_s"] = round(time.time() - t0, 1)
                if (data["userinfo_status"] != 200 or
                        any(w["status"] not in (200, 404) for w in data["windows"])):
                    receipt["fatal"] = "API requests failed"
                    receipt["userinfo_status"] = data["userinfo_status"]
                    receipt["windows"] = [
                        {"window": w["window"], "status": w["status"]}
                        for w in data["windows"]]
                    print(json.dumps(receipt, indent=1))
                    return 4
                base = os.path.join(args.out, f"extratos-{cpf_digits}")
                json.dump(data, open(base + ".json", "w"), indent=1, ensure_ascii=False)
                scalars = ["idCredito", "dtCompetenciaCredito", "nb", "dtInicioPeriodo",
                           "dtFimPeriodo", "dtPgtoCredito", "inCreditoPago", "inRetornoBanco",
                           "valorLiquido", "dib", "dip", "mr"]
                with open(base + "-creditos.csv", "w", newline="") as fh:
                    w = csv.writer(fh)
                    w.writerow(scalars + ["n_rubricas"])
                    for c in data["creditos"]:
                        w.writerow([c.get(x) for x in scalars]
                                   + [len(c.get("rubricasCreditoTO") or [])])
                with open(base + "-rubricas.csv", "w", newline="") as fh:
                    w = csv.writer(fh)
                    w.writerow(["idCredito", "codigoRubrica", "valorRubrica", "rubrica_nome"])
                    for c in data["creditos"]:
                        for r_ in c.get("rubricasCreditoTO") or []:
                            w.writerow([c["idCredito"], r_.get("codigoRubrica"),
                                        r_.get("valorRubrica"),
                                        (r_.get("rubricaTO") or {}).get("nome")])
                receipt["files"] = [base + ".json", base + "-creditos.csv",
                                    base + "-rubricas.csv"]
                receipt["windows"] = data["windows"]
                receipt["n_credits"] = data["n_unique_credits"]
            finally:
                b.close()
    finally:
        if mgr is not None:
            mgr.stop()
    receipt["total_s"] = round(time.time() - t_all, 1)
    receipt["model_cost_usd"] = round(sum(
        (r.get("model_usage") or {}).get("cost", 0) or 0
        for r in receipt.get("rounds", [])), 6)
    print(json.dumps(receipt, indent=1))
    json.dump(receipt, open(os.path.join(args.out, "receipt.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
