#!/usr/bin/env python3
"""Mechanics test: click fixed tile indices (1-9, row-major) + Avancar.
Validates grid geometry + coords + submit without any AI.
Usage: test_clicks.py --cdp URL --indices 1,3,5,7,9 --round-dir runs/t1
"""
import argparse
import json
import os
import random
import time

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
STATE_JS = """() => ({
  tok: ((document.querySelector('[name="h-captcha-response"]') || {value: ''}).value || '').length,
  pwd: !!document.querySelector('input[type="password"]'),
  frames: document.querySelectorAll('iframe[src*="hcaptcha"]').length
})"""


def click(page, x, y):
    page.mouse.move(x + random.uniform(-3, 3), y + random.uniform(-3, 3), steps=12)
    page.wait_for_timeout(random.randint(250, 500))
    page.mouse.click(x, y)
    page.wait_for_timeout(random.randint(400, 700))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9223")
    ap.add_argument("--indices", required=True)
    ap.add_argument("--round-dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.round_dir, exist_ok=True)
    idx = [int(s) for s in args.indices.split(",")]
    res = {"indices": idx}
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(args.cdp)
        try:
            ctx = b.contexts[0]
            sso = [pg for pg in ctx.pages if "sso.acesso" in pg.url]
            page = sso[0]
            boxes = page.evaluate(IFRAME_JS)
            vis = [b for b in boxes if 250 < b["w"] < 450 and 400 < b["h"] < 700
                   and -50 < b["y"] < 300 and -50 < b["x"] < 1200]
            res["boxes"] = boxes
            if not vis:
                res["abort"] = "no-visible-challenge"
                print(json.dumps(res, indent=1))
                json.dump(res, open(os.path.join(args.round_dir, "log.json"), "w"), indent=1)
                return
            boxes = vis
            clip = clamp_clip(boxes[0])
            page.screenshot(path=os.path.join(args.round_dir, "modal.png"), clip=clip)
            crop = Image.open(os.path.join(args.round_dir, "modal.png"))
            grid = find_grid(crop)
            res["grid"] = grid
            tiles = split_tiles(crop, grid)
            for i, t in enumerate(tiles):
                t.save(os.path.join(args.round_dir, f"tile{i+1}.png"))
            centers = tile_centers_page(boxes[0], clip, grid)
            res["centers"] = centers
            for n in idx:
                boxes = page.evaluate(IFRAME_JS)
                vis = [b for b in boxes if 250 < b["w"] < 450 and 400 < b["h"] < 700
                       and -50 < b["y"] < 300]
                if not vis:
                    res["stopped_at"] = n
                    break
                clip = clamp_clip(vis[0])
                crop = Image.open(os.path.join(args.round_dir, "modal.png"))
                # re-screenshot for fresh geometry (scroll drift proof)
                page.screenshot(path=os.path.join(args.round_dir, "modal.png"), clip=clip)
                crop = Image.open(os.path.join(args.round_dir, "modal.png"))
                grid = find_grid(crop)
                centers = tile_centers_page(vis[0], clip, grid)
                x, y = centers[n - 1]
                click(page, x, y)
            page.screenshot(path=os.path.join(args.round_dir, "selected.png"))
            ax = clip["x"] + crop.width - 58
            ay = clip["y"] + crop.height - 38
            res["avancar"] = [ax, ay]
            click(page, ax, ay)
            page.wait_for_timeout(8000)
            page.screenshot(path=os.path.join(args.round_dir, "after.png"))
            res["state_after"] = page.evaluate(STATE_JS)
        finally:
            b.close()
    print(json.dumps(res, indent=1))
    json.dump(res, open(os.path.join(args.round_dir, "log.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
