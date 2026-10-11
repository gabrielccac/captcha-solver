#!/usr/bin/env python3
"""Full run tail: password submit (human-like) -> tokens -> requests extraction.
Env: GOVBR_PASSWORD, OUT_CPF_FULL (digits only). Writes OUT_DIR/extratos-<cpf>.json + csvs.
Usage: full_run.py --cdp URL --cpf-full 06284796200 --out-dir runs/full2out
"""
import argparse, csv, json, os, random, sys
import requests
from playwright.sync_api import sync_playwright

def human_type(page, selector, text):
    loc = page.locator(selector).first
    box = loc.bounding_box(timeout=15000)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=25)
    page.wait_for_timeout(random.randint(300, 700))
    loc.click(timeout=10000)
    page.wait_for_timeout(random.randint(400, 900))
    for ch in text:
        page.keyboard.type(ch, delay=random.randint(90, 220))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9223")
    ap.add_argument("--cpf-full", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    pwd = os.environ.get("GOVBR_PASSWORD", "")
    if not pwd:
        print(json.dumps({"fatal": "GOVBR_PASSWORD empty"})); return
    res = {}
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(args.cdp)
        try:
            ctx = b.contexts[0]
            pages = [pg for pg in ctx.pages if "sso.acesso" in pg.url]
            if not pages:
                print(json.dumps({"fatal": "no sso page"})); return
            page = pages[0]
            human_type(page, 'input[type="password"]', pwd)
            page.wait_for_timeout(random.randint(600, 1200))
            btn = page.locator('button:has-text("Continuar"), button[type="submit"]').first
            bx = btn.bounding_box(timeout=10000)
            page.mouse.move(bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2, steps=30)
            page.wait_for_timeout(random.randint(400, 900))
            page.mouse.click(bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2)
            page.wait_for_timeout(12000)
            page.screenshot(path=os.path.join(args.out_dir, "after-pass.png"))
            res["landed"] = page.url.split("?")[0][:100]
            # wait for app tokens (SPA fills localStorage async)
            vals = None
            for _ in range(20):
                vals = page.evaluate("() => ({auth: localStorage.getItem('ifs_auth'), mi: localStorage.getItem('miToken')})")
                if vals["auth"] and vals["mi"]:
                    break
                page.wait_for_timeout(1000)
            res["has_auth"] = bool(vals and vals["auth"])
            res["has_mi"] = bool(vals and vals["mi"])
            if not (vals and vals["auth"] and vals["mi"]):
                print(json.dumps(res, indent=1)); return
            auth, mi = vals["auth"], vals["mi"]
        finally:
            b.close()
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
                      "Accept": "application/json", "Referer": "https://meu.inss.gov.br/"})
    h = {"Authorization": "Bearer " + auth, "mitoken": mi}
    cpf_full = args.cpf_full
    cpf_id = cpf_full.lstrip("0")
    out = {"windows": []}
    r = s.get(f"https://vip-pmeuinss-api.inss.gov.br/apis/usuarioservices/info/{cpf_full}?refreshPedidos=false", headers=h, timeout=30)
    out["userinfo_status"] = r.status_code
    windows = [("01-09-2026", "31-10-2026"), ("01-12-2025", "31-10-2026"),
               ("01-11-2025", "31-10-2026"),
               ("01-11-2024", "31-10-2025"), ("01-11-2023", "31-10-2024")]
    all_credits, nbs = {}, None
    for a, b_ in windows:
        rr = s.get(f"https://vip-pmeuinss-api.inss.gov.br/apis/hiscreServices/historicocreditos/{cpf_id}/{a}/{b_}", headers=h, timeout=30)
        w = {"window": f"{a}/{b_}", "status": rr.status_code}
        if rr.status_code == 200:
            body = rr.json()
            w["n_credits"] = len(body.get("creditosTO", []))
            nbs = body.get("listaNbHiscreTO", [])
            for c in body.get("creditosTO", []):
                all_credits[c["idCredito"]] = c
        else:
            w["body"] = rr.text[:100]
        out["windows"].append(w)
    out["n_unique_credits"] = len(all_credits)
    out["beneficios"] = nbs
    out["creditos"] = [all_credits[k] for k in sorted(all_credits)]
    base = os.path.join(args.out_dir, f"extratos-{cpf_full}")
    json.dump(out, open(base + ".json", "w"), indent=1, ensure_ascii=False)
    scalars = ["idCredito", "dtCompetenciaCredito", "nb", "dtInicioPeriodo", "dtFimPeriodo",
               "dtPgtoCredito", "inCreditoPago", "inRetornoBanco", "valorLiquido", "dib", "dip", "mr"]
    with open(base + "-creditos.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(scalars + ["n_rubricas"])
        for k in sorted(all_credits):
            c = all_credits[k]
            w.writerow([c.get(x) for x in scalars] + [len(c.get("rubricasCreditoTO") or [])])
    with open(base + "-rubricas.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["idCredito", "codigoRubrica", "valorRubrica", "rubrica_nome"])
        for k in sorted(all_credits):
            for r_ in all_credits[k].get("rubricasCreditoTO") or []:
                w.writerow([k, r_.get("codigoRubrica"), r_.get("valorRubrica"),
                            (r_.get("rubricaTO") or {}).get("nome")])
    print(json.dumps({"landed": res.get("landed"), "windows": out["windows"],
                      "n_unique": out["n_unique_credits"], "files": base + ".json/csvs"}, indent=1))

if __name__ == "__main__":
    main()
