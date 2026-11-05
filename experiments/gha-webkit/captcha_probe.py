"""Observe the approved worker; save only captcha task/grid pixels after it exits."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import runpy
import sys
import time

sys.path.insert(0, '/app/experiments/gha-webkit')
from captcha import hcaptcha, interactions, prefetch, solver

rounds, models, decisions, clips = [], [], [], []
original_prepare = interactions.DynamicInteractions.prepare
original_marks = interactions.selection_marks
original_click = interactions.DynamicInteractions.click
original_model = solver.solve
original_decision = prefetch.PrefetchedSolver.__call__
original_clip = hcaptcha.modal_clip
original_wait = interactions.DynamicInteractions.wait_ready


def prepare(self, before, grid, selected):
    original_prepare(self, before, grid, selected)
    rounds.append(dict(selected=sorted(selected), grid=list(grid), size=list(before.size),
                       valid_grid=(0 <= grid[0] < grid[0] + grid[2] <= before.width
                                   and 0 <= grid[1] < grid[1] + grid[3] <= before.height),
                       before=before.copy(), frames=[], clicks=[], start=time.monotonic()))


def marks(before, after, grid):
    found = original_marks(before, after, grid)
    if rounds:
        rounds[-1]['frames'].append(dict(observed=sorted(found), image=after.copy(),
            late=bool(rounds[-1].get('late_probe')),
            elapsed_ms=round((time.monotonic() - rounds[-1]['start']) * 1000)))
        if len(rounds[-1]['frames']) > 16:
            rounds[-1]['frames'].pop(0)
    return found


def wait_ready(self, page, clip):
    confirmed = original_wait(self, page, clip)
    if not confirmed and rounds:
        # Observe late rendering after failure, preserving the original failure result.
        rounds[-1]['late_probe'] = True
        try:
            for _ in range(3):
                page.wait_for_timeout(200)
                from PIL import Image
                current = Image.open(io.BytesIO(page.screenshot(clip=clip, timeout=2000))).convert('RGB')
                marks(self.before, current, self.grid)
        except Exception:
            pass
    return confirmed


def click(self, page, x, y):
    if rounds:
        rounds[-1]['clicks'].append([x, y])
    return original_click(self, page, x, y)


def model(header, tiles, *args, **kwargs):
    record = dict(batch=bool(kwargs.get('batch')), count=len(tiles), header=header.copy(),
                  tiles=[tile.copy() for tile in tiles], reference=kwargs.get('ref_img'))
    if record['reference'] is not None:
        record['reference'] = record['reference'].copy()
    models.append(record)
    start = time.monotonic()
    try:
        picked = original_model(header, tiles, *args, **kwargs)
        record['selected'] = picked
        return picked
    except Exception as error:
        record['error_type'] = type(error).__name__
        raise
    finally:
        record['elapsed_ms'] = round((time.monotonic() - start) * 1000)


def decision(self, *args, **kwargs):
    before = self.statistics()
    result = original_decision(self, *args, **kwargs)
    after = self.statistics()
    decisions.append(dict(selected=list(result), model_called=after['model_calls'] > before['model_calls'],
                          cache_hit=after['cache_hits'] > before['cache_hits'],
                          fallback=after['fallback_calls'] > before['fallback_calls']))
    return result


def modal_clip(*args, **kwargs):
    result = original_clip(*args, **kwargs)
    if len(clips) < 80:
        clips.append(dict(result))
    return result


def save(directory):
    directory.mkdir(exist_ok=True)
    metadata = dict(rounds=[], model_calls=[], decisions=decisions, clips=clips)
    for index, record in enumerate(rounds):
        x, y, width, height = record['grid']
        area = (x, y, x + width, y + height)
        if record['valid_grid']:
            record['before'].crop(area).save(directory / f'round-{index}-before-grid.png')
        frames = []
        for number, frame in enumerate(record['frames']):
            if record['valid_grid'] and frame['image'].size == record['before'].size:
                frame['image'].crop(area).save(directory / f'round-{index}-frame-{number}-grid.png')
            frames.append({k: v for k, v in frame.items() if k != 'image'})
        metadata['rounds'].append({**{k: record[k] for k in ('selected', 'grid', 'size', 'clicks', 'valid_grid')}, 'frames': frames})
    for index, record in enumerate(models):
        record['header'].save(directory / f'model-{index}-task.png')
        if record['reference'] is not None:
            record['reference'].save(directory / f'model-{index}-reference.png')
        for number, tile in enumerate(record['tiles'], 1):
            tile.save(directory / f'model-{index}-candidate-{number}.png')
        metadata['model_calls'].append({k: v for k, v in record.items()
                                       if k not in ('header', 'reference', 'tiles')})
    (directory / 'captcha-observations.json').write_text(json.dumps(metadata, indent=2))
    for path in directory.iterdir():
        path.chmod(0o644)
    return metadata


if __name__ == '__main__':
    interactions.DynamicInteractions.prepare = prepare
    interactions.DynamicInteractions.click = click
    interactions.DynamicInteractions.wait_ready = wait_ready
    interactions.selection_marks = marks
    solver.solve = hcaptcha.solve = model
    prefetch.PrefetchedSolver.__call__ = decision
    hcaptcha.modal_clip = modal_clip
    result = dict(success=False, code='probe_runtime_error')
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            worker = runpy.run_path('/app/experiments/gha-webkit/smoke.py', run_name='approved_smoke')
            result = worker['main']()
    except Exception as error:
        result['error_type'] = type(error).__name__
    try:
        result['captcha_probe'] = save(Path('/diagnostics'))
    except Exception as error:
        result['probe_save_error_type'] = type(error).__name__
    print(json.dumps(result), flush=True)
    raise SystemExit(0 if result['success'] else 3)
