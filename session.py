"""Gov.br session leg: authorize -> CPF submit -> password submit -> token harvest.
Pure CDP-page automation (caller owns browser/page). No secrets on disk.
"""
import random
import re

import requests

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
WINDOWS = [("01-09-2026", "31-10-2026"), ("01-12-2025", "31-10-2026"),
           ("01-11-2025", "31-10-2026"), ("01-11-2024", "31-10-2025"),
           ("01-11-2023", "31-10-2024")]


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


def submit_password(page, password):
    human_type(page, 'input[type="password"]', password)
    page.wait_for_timeout(random.randint(600, 1200))
    btn = page.locator('button:has-text("Continuar"), button[type="submit"]').first
    bx = btn.bounding_box(timeout=10000)
    page.mouse.move(bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2, steps=30)
    page.wait_for_timeout(random.randint(400, 900))
    page.mouse.click(bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2)
    # poll: app landing (URL leaves sso) instead of fixed 12s
    wait_for(page, "window.location.href.includes('meu.inss.gov.br')", timeout_s=30,
             desc="app landing")


def harvest_tokens(page, timeout_s=20):
    vals = {"auth": None, "mi": None}
    for _ in range(timeout_s):
        vals = page.evaluate("() => ({auth: localStorage.getItem('ifs_auth'),"
                             " mi: localStorage.getItem('miToken')})")
        if vals["auth"] and vals["mi"]:
            break
        page.wait_for_timeout(1000)
    return vals


def api_session(auth, mi):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json",
                      "Referer": "https://meu.inss.gov.br/"})
    return s, {"Authorization": "Bearer " + auth, "mitoken": mi}


def extract(cpf_full, auth, mi, windows=None):
    s, h = api_session(auth, mi)
    cpf_id = cpf_full.lstrip("0")
    out = {"windows": [], "creditos": [], "beneficios": None}
    r = s.get(f"{API}/usuarioservices/info/{cpf_full}?refreshPedidos=false",
              headers=h, timeout=30)
    out["userinfo_status"] = r.status_code
    credits = {}
    for a, b in (windows or WINDOWS):
        rr = s.get(f"{API}/hiscreServices/historicocreditos/{cpf_id}/{a}/{b}",
                   headers=h, timeout=30)
        w = {"window": f"{a}/{b}", "status": rr.status_code}
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
