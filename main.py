#!/usr/bin/env python3
"""captcha-solver main loop: extract challenge -> OpenRouter vision -> CDP clicks.
Usage: main.py --cdp http://127.0.0.1:9223 --model <openrouter-id> --round-dir runs/r1 [--rounds 3]
Env: OPENROUTER_API_KEY. Stops when token minted or password stage reached.
"""
import argparse
import json
import os
import random
import time

from PIL import Image
from playwright.sync_api import sync_playwright

from extractor import clamp_clip, find_grid, split_header, split_tiles, tile_centers_page
from solver import solve

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
    page.wait_for_timeout(random.randint(400, 800))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cdp", default="http://127.0.0.1:9223")
    ap.add_argument("--model", required=True)
    ap.add_argument("--round-dir", required=True)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()
    os.makedirs(args.round_dir, exist_ok=True)
    log = []
    with sync_playwright() as p:
        b = p.chromium.connect_over_cdp(args.cdp)
        try:
            ctx = b.contexts[0]
            sso = [pg for pg in ctx.pages if "sso.acesso" in pg.url]
            if not sso:
                print(json.dumps({"fatal": "no sso page"}))
                return
            page = sso[0]
            for rnd in range(args.rounds):
                st = page.evaluate(STATE_JS)
                if st["tok"] > 0 or st["pwd"]:
                    log.append({"round": rnd, "done": True, "state": st})
                    break
                boxes = page.evaluate(IFRAME_JS)
                if not boxes:
                    log.append({"round": rnd, "done": False, "why": "no-iframe"})
                    break
                clip = clamp_clip(boxes[0])
                shot = os.path.join(args.round_dir, f"r{rnd}-modal.png")
                page.screenshot(path=shot, clip=clip)
                crop = Image.open(shot)
                grid = find_grid(crop)
                text_img, ref_img = split_header(crop, grid[1])
                text_img.save(os.path.join(args.round_dir, f"r{rnd}-text.png"))
                entry_ref = None
                if ref_img is not None:
                    ref_img.save(os.path.join(args.round_dir, f"r{rnd}-ref.png"))
                    entry_ref = True
                tiles = split_tiles(crop, grid)
                for i, t in enumerate(tiles):
                    t.save(os.path.join(args.round_dir, f"r{rnd}-tile{i+1}.png"))
                centers = tile_centers_page(boxes[0], clip, grid)
                idx = solve(text_img, tiles, args.model, ref_img=ref_img)
                entry = {"round": rnd, "grid": grid, "has_ref": entry_ref, "model_picks": idx}
                for n in idx:  # 1-based -> centers (geometry refreshed per click)
                    boxes = page.evaluate(IFRAME_JS)
                    vis = [b for b in boxes if 250 < b["w"] < 450 and 400 < b["h"] < 700
                           and -50 < b["y"] < 300]
                    if not vis:
                        entry["stopped_at"] = n
                        break
                    clip = clamp_clip(vis[0])
                    page.screenshot(path=os.path.join(args.round_dir, f"r{rnd}-fresh.png"),
                                    clip=clip)
                    crop = Image.open(os.path.join(args.round_dir, f"r{rnd}-fresh.png"))
                    grid = find_grid(crop)
                    centers = tile_centers_page(vis[0], clip, grid)
                    x, y = centers[n - 1]
                    click(page, x, y)
                # Avancar: bottom-right of modal
                ax = clip["x"] + crop.width - 58
                ay = clip["y"] + crop.height - 38
                click(page, ax, ay)
                page.wait_for_timeout(8000)
                page.screenshot(path=os.path.join(args.round_dir, f"r{rnd}-after.png"))
                entry["state_after"] = page.evaluate(STATE_JS)
                log.append(entry)
                if entry["state_after"]["tok"] > 0 or entry["state_after"]["pwd"]:
                    break
        finally:
            b.close()
    print(json.dumps(log, indent=1))
    json.dump(log, open(os.path.join(args.round_dir, "log.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
