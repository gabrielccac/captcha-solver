"""Validate Comprasnet login through its GOV.BR SSO button."""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from captcha.browser import ManagedBrowser
from captcha.hcaptcha import page_state, solve_round, vis_modal
from inss.session import LoginError, submit_cpf, submit_password
from inss.workflow import find_page, load_dotenv, log

START_URL = "https://www.comprasnet.gov.br/seguro/loginPortalFornecedor.asp"
LANDING_HOST = "www.comprasnet.gov.br"
LANDING_PATH = "/intro.htm"
PASSWORD_SELECTOR = 'input[type="password"]'


def in_comprasnet(page):
    url = urlparse(page.url)
    return url.hostname == LANDING_HOST and url.path.lower() == LANDING_PATH


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="meta/muse-spark-1.3")
    ap.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high"))
    ap.add_argument("--max-rounds", type=int, default=3)
    args = ap.parse_args()
    os.umask(0o077)
    load_dotenv()
    cpf = "".join(c for c in args.cpf if c.isdigit())
    if len(cpf) != 11:
        ap.error("CPF must have 11 digits")
    password = os.environ.get("COMPRASNET_PASSWORD")
    if not password or not os.environ.get("OPENROUTER_API_KEY"):
        ap.error("COMPRASNET_PASSWORD and OPENROUTER_API_KEY are required")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    receipt = {"cpf_mask": f"{cpf[:3]}.***.***-{cpf[-2:]}", "model": args.model,
               "rounds": [], "success": False}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="comprasnet-chrome-",
                                     ignore_cleanup_errors=True) as profile:
        browser = ManagedBrowser(profile_dir=profile)
        cdp = browser.start()
        try:
            with sync_playwright() as p:
                chrome = p.chromium.connect_over_cdp(cdp)
                try:
                    page = chrome.contexts[0].pages[0]
                    page.goto(START_URL, wait_until="domcontentloaded", timeout=45000)
                    page.get_by_role("button", name="Entrar com Gov.br").click()
                    page.wait_for_url("**/sso.acesso.gov.br/**", timeout=30000)
                    page.wait_for_selector('input[name="accountId"], input#accountId')
                    submit_cpf(page, cpf)
                    log("Comprasnet CPF submitted")
                    deadline = time.monotonic() + 25
                    while time.monotonic() < deadline and not vis_modal(page):
                        if page_state(page, PASSWORD_SELECTOR).get("done"):
                            break
                        page.wait_for_timeout(500)
                    if not vis_modal(page) and not page_state(page, PASSWORD_SELECTOR).get("done"):
                        raise RuntimeError("captcha challenge did not appear")
                    rnd = 0
                    retry_deadline = time.monotonic() + 25
                    while rnd < args.max_rounds:
                        page = find_page(chrome, "sso.acesso") or page
                        if page_state(page, PASSWORD_SELECTOR).get("done"):
                            break
                        done, info = solve_round(page, str(out), rnd, args.model,
                                                 args.reasoning_effort, PASSWORD_SELECTOR)
                        receipt["rounds"].append(info)
                        log(f"Comprasnet captcha round {rnd}: {info.get('why', info.get('state_after'))}")
                        if done or page_state(page, PASSWORD_SELECTOR).get("done"):
                            break
                        if info.get("why") == "interrupted-clicks":
                            raise RuntimeError("captcha clicks interrupted")
                        if (info.get("why") in ("no-modal", "shot-fail", "tiles-not-ready")
                                and time.monotonic() < retry_deadline):
                            continue
                        rnd += 1
                        retry_deadline = time.monotonic() + 25
                        if not info.get("next_round_ready"):
                            page.wait_for_timeout(3000)
                    page = find_page(chrome, "sso.acesso") or page
                    if not page_state(page, PASSWORD_SELECTOR).get("done"):
                        raise RuntimeError("captcha did not reach password stage")
                    try:
                        submit_password(page, password, LANDING_HOST)
                    except LoginError as error:
                        receipt.update(fatal=str(error), error_code=error.code,
                                       login_evidence=error.evidence)
                        (out / "receipt.json").write_text(json.dumps(receipt, indent=1))
                        print(json.dumps(receipt, indent=1))
                        return 3
                    app = find_page(chrome, LANDING_HOST) or page
                    if not in_comprasnet(app):
                        app.wait_for_url("**/intro.htm*", timeout=30000)
                    if not in_comprasnet(app):
                        raise RuntimeError("Comprasnet landing URL not reached")
                    receipt["success"] = True
                    receipt["landing_url"] = app.url
                finally:
                    chrome.close()
        finally:
            processes = list(browser.procs)
            browser.stop()
            for process in processes:
                process.wait(timeout=10)
            for _ in range(10):
                try:
                    shutil.rmtree(profile)
                    break
                except OSError:
                    time.sleep(0.5)
            else:
                log("temporary Chrome profile cleanup incomplete")
    receipt["total_s"] = round(time.monotonic() - started, 1)
    receipt["model_cost_usd"] = round(sum(
        (r.get("model_usage") or {}).get("cost", 0) or 0
        for r in receipt["rounds"]), 6)
    (out / "receipt.json").write_text(json.dumps(receipt, indent=1))
    print(json.dumps(receipt, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
