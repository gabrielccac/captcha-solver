#!/usr/bin/env python3
"""Click ALL 9 tiles one by one, screenshot after each (visible proof).
No submit (no Avancar click). Usage: click_all.py --cdp URL --round-dir runs/a9
"""
import argparse
import json
import os
import random

from PIL import Image
from playwright.sync_api import sync_playwright

from extractor import clamp_clip, find_grid, split_tiles, tile_centers_page

IFRAME_JS = """() => {
  const out = [];
  document.querySelectorAll('iframe').forEach((f) => {
    const r = f.getBoundingClientRect();
    if ((f.src || '').includes('hcaptcha') && r.width > 50)
      out.push({x: r.x, y: r.y, w: r.width, h: r.height});
  });
  return out;
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9223")
    ap.add_argument("--round-dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.round_dir, exist_ok=True)
    res = {}
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(args.cdp)
        try:
            ctx = b.contexts[0]
            page = [pg for pg in ctx.pages if "sso.acesso" in pg.url][0]
            boxes = page.evaluate(IFRAME_JS)
            vis = [x for x in boxes if 250 < x["w"] < 450 and 400 < x["h"] < 700
                   and -50 < x["y"] < 300]
            if not vis:
                res["abort"] = "no-visible-challenge"
                print(json.dumps(res))
                return
            clip = clamp_clip(vis[0])
            page.screenshot(path=os.path.join(args.round_dir, "modal.png"), clip=clip)
            crop = Image.open(os.path.join(args.round_dir, "modal.png"))
            grid = find_grid(crop)
            res["grid"] = grid
            centers = tile_centers_page(vis[0], clip, grid)
            shots = []
            for n in range(1, 10):
                # geometry refresh every click: page scroll/layout shifts stale coords
                boxes = page.evaluate(IFRAME_JS)
                vis = [x for x in boxes if 250 < x["w"] < 450 and 400 < x["h"] < 700
                       and -50 < x["y"] < 300]
                if not vis:
                    res["stopped_at"] = n
                    break
                clip = clamp_clip(vis[0])
                shot = os.path.join(args.round_dir, f"pre{n}.png")
                page.screenshot(path=shot, clip=clip)
                crop = Image.open(shot)
                grid = find_grid(crop)
                centers = tile_centers_page(vis[0], clip, grid)
                x, y = centers[n - 1]
                page.mouse.move(x + random.uniform(-3, 3), y + random.uniform(-3, 3), steps=12)
                page.wait_for_timeout(random.randint(250, 450))
                page.mouse.click(x, y)
                page.wait_for_timeout(700)
                fp = os.path.join(args.round_dir, f"click{n}.png")
                page.screenshot(path=fp, clip=clip)
                shots.append(fp)
            # montage 3x3 of modal crops after each click
            ims = [Image.open(s) for s in shots]
            w, h = ims[0].size
            sc = 0.5
            small = [im.resize((int(w * sc), int(h * sc))) for im in ims]
            sw, sh = small[0].size
            sheet = Image.new("RGB", (sw * 3 + 20, sh * 3 + 20), "black")
            for i, im in enumerate(small):
                sheet.paste(im, ((i % 3) * (sw + 10), (i // 3) * (sh + 10)))
            sheet.save(os.path.join(args.round_dir, "strip.png"))
            res["shots"] = len(shots)
        finally:
            b.close()
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
