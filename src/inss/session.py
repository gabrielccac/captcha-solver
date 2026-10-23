"""Gov.br session leg: authorize -> CPF submit -> password submit -> token harvest.
Pure CDP-page automation (caller owns browser/page). No secrets on disk.
"""
import base64
import json
import random
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from inss.concession import parse_concession_pdf

AUTHZ = ("https://sso.acesso.gov.br/authorize?response_type=code"
         "&client_id=autorizar.meu.inss.gov.br"
         "&scope=openid+email+profile+govbr_confiabilidades_idtoken"
         "+poav1_createPoaAccessHistory_agent"
         "+poav1_retrievePowersOfAttorneyByAgentAccountIdAndGrantorAccountIdAndClientIdAndIsActive_agent"
         "&redirect_uri=https%3A%2F%2Fmeu.inss.gov.br%2F"
         "&nonce={nonce}&state={state}")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/146.0.0.0 Safari/537.36")
API = "https://vip-pmeuinss-api.inss.gov.br/apis"


def token_claims(token):
    """Read claims from the current session; this does not verify a JWT signature."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def token_validity(auth, mi, now=None):
    now = int(time.time()) if now is None else now
    result = {"checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat()}
    for name, token in (("authorization", auth), ("mi_token", mi)):
        claims = token_claims(token)
        issued, expires = claims.get("iat"), claims.get("exp")
        result[name] = {
            "issued_at": datetime.fromtimestamp(issued, timezone.utc).isoformat() if issued else None,
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat() if expires else None,
            "declared_lifetime_seconds": expires - issued if expires and issued else None,
            "remaining_seconds": max(0, expires - now) if expires else None,
        }
    remaining = [v["remaining_seconds"] for v in (result["authorization"], result["mi_token"])]
    result["session_remaining_seconds"] = min(remaining) if None not in remaining else None
    return result


def credit_windows(today=None, years=5):
    today = today or date.today()
    windows = []
    for offset in range(years):
        month = date(today.year - offset, today.month, 1)
        next_month = (date(month.year + 1, 1, 1) if month.month == 12 else
                      date(month.year, month.month + 1, 1))
        start = date(next_month.year - 1, next_month.month, 1)
        end = today if offset == 0 else next_month - timedelta(days=1)
        windows.append((start.strftime("%d-%m-%Y"), end.strftime("%d-%m-%Y")))
    return windows


def human_type(page, selector, text):
    loc = page.locator(selector).first
    box = loc.bounding_box(timeout=15000)
    page.mouse.move(box["x"] + box["width"] / 2 + random.uniform(-20, 20),
                    box["y"] + box["height"] / 2 + random.uniform(-3, 3), steps=25)
    page.wait_for_timeout(random.randint(300, 700))
    loc.click(timeout=10000)
    page.wait_for_timeout(random.randint(400, 900))
    for ch in text:
        page.keyboard.type(ch, delay=random.randint(90, 220))


def wait_for(page, js_cond, timeout_s=20, poll_ms=500, desc=""):
    """Dynamic poll: return True when js_cond is truthy, False on timeout."""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if page.evaluate(f"() => !!({js_cond})"):
                return True
        except Exception:
            pass
        page.wait_for_timeout(poll_ms)
    return False


def goto_login(page):
    page.goto(AUTHZ.format(nonce=random.random() * 10, state=random.random() * 10),
              wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector('input[name="accountId"], input#accountId', timeout=20000)
    m = re.search(r"authorization_id=([a-z0-9]+)", page.url)
    return m.group(1) if m else None


def submit_cpf(page, cpf_masked):
    human_type(page, 'input[name="accountId"], input#accountId', cpf_masked)
    page.wait_for_timeout(random.randint(800, 1400))
    btn = page.locator('button:has-text("Continuar")').first
    box = btn.bounding_box(timeout=10000)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=30)
    page.wait_for_timeout(random.randint(500, 1000))
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def submit_password(page, password, landing_host="meu.inss.gov.br"):
    human_type(page, 'input[type="password"]', password)
    page.wait_for_timeout(random.randint(600, 1200))
    btn = page.locator('button:has-text("Continuar"), button[type="submit"]').first
    bx = btn.bounding_box(timeout=10000)
    page.mouse.move(bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2, steps=30)
    page.wait_for_timeout(random.randint(400, 900))
    page.mouse.click(bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2)
    # poll: app landing (URL leaves sso) instead of fixed 12s
    wait_for(page, f"window.location.href.includes('{landing_host}')", timeout_s=30,
             desc="app landing")


def harvest_tokens(page, cpf_full, timeout_s=20):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            vals = page.evaluate("() => ({auth: localStorage.getItem('ifs_auth'),"
                                 " mi: localStorage.getItem('miToken')})")
        except Exception:
            vals = {}
        if vals.get("auth") and vals.get("mi"):
            s, h = api_session(vals["auth"], vals["mi"])
            try:
                r = s.get(f"{API}/usuarioservices/info/{cpf_full}?refreshPedidos=false",
                          headers=h, timeout=3)
                if r.status_code == 200:
                    return vals
            except requests.RequestException:
                pass
            finally:
                s.close()
        page.wait_for_timeout(1000)
    return {"auth": None, "mi": None}


def api_session(auth, mi):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Referer": "https://meu.inss.gov.br/"})
    return s, {"Authorization": "Bearer " + auth, "mitoken": mi}


def concession_letters(auth, mi, output_dir):
    benefits = token_claims(mi).get("nbs", [])
    if not isinstance(benefits, list) or any(not re.fullmatch(r"[0-9]{1,15}", str(nb)) for nb in benefits):
        raise ValueError("Invalid benefit numbers in session")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with api_session(auth, mi)[0] as session:
        headers = dict(session.headers, Authorization="Bearer " + auth, mitoken=mi)

    def download(nb):
        result = {"benefit_number": nb, "http_status": None, "file": None}
        started = time.monotonic()
        try:
            response = requests.get(f"{API}/calculoBeneficioServices/concalPdf/{nb}",
                                    headers=headers, timeout=30, allow_redirects=False)
            result["http_status"] = response.status_code
            if response.status_code != 200:
                result["error"] = "Carta unavailable" if response.status_code == 404 else "Carta request failed"
            elif not response.content.startswith(b"%PDF-"):
                result["error"] = "Response is not a PDF"
            else:
                filename = f"carta-concessao-{nb}.pdf"
                path = output_dir / filename
                with path.open("wb") as fh:
                    path.chmod(0o600)
                    fh.write(response.content)
                result.update(file=filename, size_bytes=len(response.content))
                try:
                    result["parsed"] = parse_concession_pdf(path)
                except (OSError, subprocess.SubprocessError):
                    result["parse_error"] = "PDF text extraction failed; check pdftotext installation"
        except requests.RequestException:
            result["error"] = "Carta request timed out or could not connect"
        result["elapsed_s"] = round(time.monotonic() - started, 1)
        return result

    with ThreadPoolExecutor(max_workers=5) as pool:
        return list(pool.map(download, dict.fromkeys(map(str, benefits))))


def extract(cpf_full, auth, mi, windows=None):
    s, h = api_session(auth, mi)
    cpf_id = cpf_full.lstrip("0")
    out = {"windows": [], "creditos": [], "beneficios": None}
    windows = windows or credit_windows()
    headers = dict(s.headers, **h)

    def get_window(window):
        a, b = window
        started = time.monotonic()
        rr = requests.get(f"{API}/hiscreServices/historicocreditos/{cpf_id}/{a}/{b}",
                          headers=headers, timeout=30)
        return rr, round(time.monotonic() - started, 1)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(get_window, window) for window in windows]
        started = time.monotonic()
        r = s.get(f"{API}/usuarioservices/info/{cpf_full}?refreshPedidos=false",
                  headers=h, timeout=30)
        out["userinfo_status"] = r.status_code
        out["userinfo_s"] = round(time.monotonic() - started, 1)
        credits = {}
        for (a, b), future in zip(windows, futures):
            rr, elapsed = future.result()
            w = {"window": f"{a}/{b}", "status": rr.status_code,
                 "elapsed_s": elapsed}
            if rr.status_code == 200:
                body = rr.json()
                w["n_credits"] = len(body.get("creditosTO", []))
                out["beneficios"] = body.get("listaNbHiscreTO", [])
                for c in body.get("creditosTO", []):
                    credits[c["idCredito"]] = c
            else:
                w["body"] = rr.text[:100]
            out["windows"].append(w)
    out["n_unique_credits"] = len(credits)
    out["creditos"] = [credits[k] for k in sorted(credits)]
    return out
