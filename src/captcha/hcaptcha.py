"""Capture and solve one 3x3 hCaptcha image challenge on a Playwright page."""
import io
import os
import random
import sys
import time

from PIL import Image, ImageChops, ImageStat

from .extractor import clamp_clip, find_grid, split_header, split_tiles, tile_centers_page
from .solver import solve


IFRAME_JS = """() => {
  const out = [];
  document.querySelectorAll('iframe').forEach((f) => {
    const r = f.getBoundingClientRect();
    if ((f.src || '').includes('hcaptcha') && r.width > 50)
      out.push({x: r.x, y: r.y, w: r.width, h: r.height});
  });
  return out;
}"""
STATE_JS = """(successSelector) => ({
  tok: ((document.querySelector('[name="h-captcha-response"]') || {value: ''}).value || '').length,
  done: !!(successSelector && document.querySelector(successSelector)),
  frames: document.querySelectorAll('iframe[src*="hcaptcha"]').length
})"""
VIEWPORT_JS = "() => ({width: innerWidth, height: innerHeight})"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def page_state(page, success_selector=None):
    try:
        return page.evaluate(STATE_JS, success_selector)
    except Exception:
        return {"tok": 0, "done": False, "frames": -1, "ctx_dead": True}


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
    return all(not (min(stat.mean) > 240 and max(stat.stddev) < 2)
               for stat in stats)


def solve_round(page, out_dir, rnd, model, reasoning_effort=None,
                success_selector=None, *, api_key=None, interactions=None, vision_solver=None):
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
        if (tiles_ready(crop, grid)
                and (interactions is None or interactions.images_ready(crop,grid))
                and (vision_solver is None or vision_solver.images_ready(crop,grid))):
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
        kwargs = {"api_key": api_key} if api_key is not None else {}
        solve_images = vision_solver if vision_solver is not None else solve
        idx = solve_images(text_img, tiles, model, ref_img=ref_img,
                    reasoning_effort=reasoning_effort, usage=usage, **kwargs)
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
    if interactions is not None:
        interactions.prepare(crop,grid,idx)
    click_target=interactions.click if interactions is not None else click
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
        click_target(page, x, y)
    if "stopped_at" in info:
        return False, {**info, "why": "interrupted-clicks"}
    boxes = vis_modal(page)
    if boxes:
        clip = modal_clip(page, boxes[0])
        ax = clip["x"] + clip["width"] - 58
        ay = clip["y"] + clip["height"] - 38
        # settle: let widget register all toggles before submitting (race fix)
        if interactions is None:
            page.wait_for_timeout(2500)
        elif not interactions.wait_ready(page,clip):
            return False, {**info, "why":"selection_not_confirmed"}
        click_target(page, ax, ay)
        info["submitted"] = True
    else:
        info["submitted"] = False
    info["click_s"] = round(time.monotonic() - t0, 1)
    # poll: challenge verdict (token / caller success stage / new round) up to 15s
    t0 = time.monotonic()
    st = page_state(page, success_selector)
    deadline = time.monotonic() + 15
    candidate = None
    while time.monotonic() < deadline and not (st["tok"] > 0 or st["done"]):
        page.wait_for_timeout(500)
        st = page_state(page, success_selector)
        if st["tok"] > 0 or st["done"]:
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
    return bool(st["tok"] > 0 or st["done"]), info
