#!/usr/bin/env python3
"""One-session demo: CPF submit -> challenge -> click tiles 1-9 with proof strip.
Usage: cycle9.py --cdp URL --cpf X --round-dir runs/c9 [--cookies none|all]
"""
import argparse
import json
import os
import random
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

sys.path.insert(0, "/home/gabriel/captures/inss-meu-20261011")
from govbr_warm import human_type, load_trust, AUTHZ  # noqa: E402

from extractor import clamp_clip, find_grid, tile_centers_page  # noqa: E402

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


def vis_modal(page):
    boxes = page.evaluate(IFRAME_JS)
    return [x for x in boxes if 250 < x["w"] < 450 and 400 < x["h"] < 700
            and -50 < x["y"] < 300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9223")
    ap.add_argument("--cpf", required=True)
    ap.add_argument("--round-dir", required=True)
    ap.add_argument("--cookies", default="none", choices=["all", "hcaptcha-only", "none"])
    args = ap.parse_args()
    os.makedirs(args.round_dir, exist_ok=True)
    res = {}
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(args.cdp)
        try:
            ctx = b.contexts[0]
            if args.cookies != "none":
                ctx.add_cookies(load_trust(args.cookies))
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(AUTHZ.format(nonce=random.random() * 10, state=random.random() * 10),
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(random.randint(5000, 7000))
            human_type(page, 'input[name="accountId"], input#accountId', args.cpf)
            page.wait_for_timeout(random.randint(800, 1400))
            btn = page.locator('button:has-text("Continuar")').first
            box = btn.bounding_box(timeout=10000)
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=30)
            page.wait_for_timeout(random.randint(500, 1000))
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            # wait for modal
            modal = []
            for _ in range(30):
                page.wait_for_timeout(1000)
                modal = vis_modal(page)
                if modal:
                    break
            if not modal:
                res["abort"] = "no-challenge"
                print(json.dumps(res))
                return
            clip = clamp_clip(modal[0])
            page.screenshot(path=os.path.join(args.round_dir, "modal.png"), clip=clip)
            crop = Image.open(os.path.join(args.round_dir, "modal.png"))
            grid = find_grid(crop)
            res["grid"] = grid
            centers = tile_centers_page(modal[0], clip, grid)
            shots = []
            for n in range(1, 10):
                x, y = centers[n - 1]
                page.mouse.move(x + random.uniform(-3, 3), y + random.uniform(-3, 3), steps=12)
                page.wait_for_timeout(random.randint(250, 450))
                page.mouse.click(x, y)
                page.wait_for_timeout(900)
                fp = os.path.join(args.round_dir, f"click{n}.png")
                page.screenshot(path=fp, clip=clip)
                shots.append(fp)
            ims = [Image.open(s) for s in shots]
            w, h = ims[0].size
            sc = 0.45
            small = [im.resize((int(w * sc), int(h * sc))) for im in ims]
            sw, sh = small[0].size
            sheet = Image.new("RGB", (sw * 3 + 20, sh * 3 + 20), "black")
            for i, im in enumerate(small):
                sheet.paste(im, ((i % 3) * (sw + 10), (i // 3) * (sh + 10)))
            sheet.save(os.path.join(args.round_dir, "strip.png"))
            res["shots"] = 9
            res["state_after"] = page.evaluate(STATE_JS)
        finally:
            b.close()
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
