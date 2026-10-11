#!/usr/bin/env python3
"""Consolidated customer runner: CPF+password -> login -> extrato CSV/JSON.
One command, own browser lifecycle, dotenv in code, per-stage receipt.
Usage:
  ~/.local/bin/uv run --with playwright --with pillow --with requests --quiet \\
    python3 runner.py --cpf 062.847.962-00 --out runs/customer1 [--model ...]
Env (or .env next to this file): GOVBR_PASSWORD, OPENROUTER_API_KEY.
Exit codes: 0 ok | 2 no-challenge/solve-fail | 3 login-fail | 4 extract-fail.
"""
import argparse
import csv
import faulthandler
import hashlib
import io
import json
import os
import random
import sys
import time

faulthandler.enable()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageChops, ImageStat  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from browser import ManagedBrowser  # noqa: E402
from extractor import clamp_clip, find_grid, split_header, split_tiles  # noqa: E402
from extractor import tile_centers_page  # noqa: E402
from session import extract, goto_login, harvest_tokens  # noqa: E402
from session import submit_cpf, submit_password  # noqa: E402
from solver import solve  # noqa: E402

IFRAME_JS = """() => {
  const out = [];
  document.querySelectorAll('iframe').forEach((f) => {
    const r = f.getBoundingClientRect();
    if ((f.src || '').includes('hcaptcha') && r.width > 50)
      out.push({x: r.x, y: r.y, w: r.width, h: r.height});
  });
  return out;
}"""
STATE_JS = """() => ({
  tok: ((document.querySelector('[name="h-captcha-response"]') || {value: ''}).value || '').length,
  pwd: !!document.querySelector('input[type="password"]'),
  frames: document.querySelectorAll('iframe[src*="hcaptcha"]').length
})"""
VIEWPORT_JS = "() => ({width: innerWidth, height: innerHeight})"


def load_dotenv():
    here = os.path.dirname(os.path.abspath(__file__))
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


def page_state(page):
    try:
        return page.evaluate(STATE_JS)
    except Exception:
        return {"tok": 0, "pwd": False, "frames": -1, "ctx_dead": True}


def vis_modal(page):
    return [x for x in page.evaluate(IFRAME_JS) if 250 < x["w"] < 450
            and 400 < x["h"] < 700 and -50 < x["y"] < 300]


def modal_clip(page, box):
    viewport = page.evaluate(VIEWPORT_JS)
    return clamp_clip(box, viewport["width"], viewport["height"])


def click(page, x, y):
    page.mouse.move(x + random.uniform(-3, 3), y + random.uniform(-3, 3), steps=12)
    page.wait_for_timeout(random.randint(150, 250))
    page.mouse.click(x, y)
    page.wait_for_timeout(random.randint(250, 400))


def wait_modal(page, timeout_s=5):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        m = vis_modal(page)
        if m:
            return m
        page.wait_for_timeout(500)
    return []


def shot(page, path, clip, tries=2):
    """Screenshot with a fresh iframe clip after a failed attempt."""
    last = None
    for attempt in range(tries):
        try:
            page.screenshot(path=path, clip=clip, timeout=8000)
            return clip
        except Exception as e:
            last = e
            if attempt + 1 < tries:
                page.wait_for_timeout(500)
                boxes = vis_modal(page)
                if not boxes:
                    break
                clip = modal_clip(page, boxes[0])
    log(f"screenshot failed: {str(last)[:120]}")
    return None


def grid_difference(before, after, grid):
    if before.size != after.size:
        return 0
    gx, gy, gw, gh = grid
    area = (gx, gy, gx + gw, gy + gh)
    diff = ImageChops.difference(before.crop(area).convert("RGB"),
                                 after.crop(area).convert("RGB"))
    return sum(ImageStat.Stat(diff).mean) / 3


def tiles_ready(crop, grid):
    # Loading placeholders are near-white and nearly uniform.
    stats = (ImageStat.Stat(tile.convert("RGB")) for tile in split_tiles(crop, grid))
    return all(not (min(stat.mean) > 240 and max(stat.stddev) < 9)
               for stat in stats)


def solve_round(page, out_dir, rnd, model, reasoning_effort=None):
    """One extract->solve->click round. Returns (done_bool, info_dict)."""
    info = {"round": rnd}
    t0 = time.monotonic()
    boxes = wait_modal(page)
    info["modal_wait_s"] = round(time.monotonic() - t0, 1)
    if not boxes:
        return False, {**info, "why": "no-modal"}
    clip = modal_clip(page, boxes[0])
    shot_path = os.path.join(out_dir, f"r{rnd}-modal.png")
    t0 = time.monotonic()
    deadline = t0 + 8
    while True:
        clip = shot(page, shot_path, clip)
        if not clip:
            return False, {**info, "why": "shot-fail"}
        crop = Image.open(shot_path).copy()
        grid = find_grid(crop)
        if tiles_ready(crop, grid):
            break
        if time.monotonic() >= deadline:
            return False, {**info, "why": "tiles-not-ready"}
        page.wait_for_timeout(300)
    info["capture_s"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()
    text_img, ref_img = split_header(crop, grid[1])
    text_img.save(os.path.join(out_dir, f"r{rnd}-text.png"))
    info["has_ref"] = ref_img is not None
    if ref_img is not None:
        ref_img.save(os.path.join(out_dir, f"r{rnd}-ref.png"))
    tiles = split_tiles(crop, grid)
    for i, t in enumerate(tiles):
        t.save(os.path.join(out_dir, f"r{rnd}-tile{i+1}.png"))
    info["extract_s"] = round(time.monotonic() - t0, 1)
    t0 = time.monotonic()
    usage = {}
    try:
        idx = solve(text_img, tiles, model, ref_img=ref_img,
                    reasoning_effort=reasoning_effort, usage=usage)
    except Exception as e:
        info["solve_s"] = round(time.monotonic() - t0, 1)
        info["solve_error"] = str(e)[:200]
        if usage:
            info["model_usage"] = usage
        return False, info
    if usage:
        info["model_usage"] = usage
    info["model_picks"] = idx
    info["solve_s"] = round(time.monotonic() - t0, 1)
    if not idx:
        return False, {**info, "why": "empty-picks"}
    t0 = time.monotonic()
    for n in idx:
        boxes = wait_modal(page, timeout_s=10)
        if not boxes:
            info["stopped_at"] = n
            break
        clip = modal_clip(page, boxes[0])
        if (clip["width"], clip["height"]) != crop.size:
            fresh = os.path.join(out_dir, f"r{rnd}-geometry.png")
            clip = shot(page, fresh, clip)
            if not clip:
                info["stopped_at"] = n
                break
            crop = Image.open(fresh)
            grid = find_grid(crop)
        centers = tile_centers_page(boxes[0], clip, grid)
        x, y = centers[n - 1]
        click(page, x, y)
    if "stopped_at" in info:
        return False, {**info, "why": "interrupted-clicks"}
    boxes = vis_modal(page)
    if boxes:
        clip = modal_clip(page, boxes[0])
        ax = clip["x"] + clip["width"] - 58
        ay = clip["y"] + clip["height"] - 38
        # settle: let widget register all toggles before submitting (race fix)
        page.wait_for_timeout(2500)
        click(page, ax, ay)
        info["submitted"] = True
    else:
        info["submitted"] = False
    info["click_s"] = round(time.monotonic() - t0, 1)
    # poll: challenge verdict (token / password stage / new round) up to 15s
    t0 = time.monotonic()
    st = page_state(page)
    deadline = time.monotonic() + 15
    candidate = None
    while time.monotonic() < deadline and not (st["tok"] > 0 or st["pwd"]):
        page.wait_for_timeout(500)
        st = page_state(page)
        if st["tok"] > 0 or st["pwd"]:
            info["next_round_ready"] = True
            break
        try:
            boxes = vis_modal(page)
            if not boxes:
                continue
            fresh = Image.open(io.BytesIO(page.screenshot(
                clip=modal_clip(page, boxes[0]), timeout=2500)))
            # shortcut: full-grid changes mark a new round; use frame state if tiles refresh individually.
            if grid_difference(crop, fresh, grid) > 40:
                if candidate is not None and grid_difference(candidate, fresh, grid) < 10:
                    info["next_round_ready"] = True
                    break
                candidate = fresh
            else:
                candidate = None
        except Exception as e:
            info.setdefault("verdict_probe_error", str(e)[:120])
    info["state_after"] = st
    info["verdict_s"] = round(time.monotonic() - t0, 1)
    return bool(st["tok"] > 0 or st["pwd"]), info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cpf", required=True, help="masked or plain, e.g. 062.847.962-00")
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="meta/muse-spark-1.3")
    ap.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high"),
                    help="OpenRouter reasoning effort; omit to keep the model default")
    ap.add_argument("--password-env", default="GOVBR_PASSWORD")
    ap.add_argument("--profile-dir", default=None)
    ap.add_argument("--cdp-port", type=int, default=9223)
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

    profiles = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles")
    default_prof = os.path.join(profiles, "default")
    owner = hashlib.sha256(cpf_digits.encode()).hexdigest()
    marker = os.path.join(default_prof, ".cpf-owner")
    if args.profile_dir:
        prof = args.profile_dir
    else:
        with open(marker) if os.path.exists(marker) else io.StringIO() as fh:
            prof = default_prof if fh.read().strip() == owner else os.path.join(
                profiles, owner[:16])
    receipt["profile_dir"] = prof
    mgr = ManagedBrowser(profile_dir=prof, cdp_port=args.cdp_port)
    t0 = time.time()
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
                        early = page_state(page)
                    except Exception:
                        early = {}
                    if early.get("pwd"):
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
                                                 args.reasoning_effort)
                    except Exception as e:
                        info = {"round": rnd, "crashed": str(e)[:200]}
                        done = False
                        log(f"round {rnd} crashed: {str(e)[:150]}")
                        # navigation during solve often MEANS success: re-check.
                        page.wait_for_timeout(3000)
                        page = find_page(b, "sso.acesso") or find_page(b, "meu.inss") or page
                        rec = page_state(page)
                        if rec.get("pwd") or (page is not None
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
