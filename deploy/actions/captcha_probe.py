"""Capture captcha pixels and decisions for one diagnostic login.

Enabled only by ``INSS_DIAG_DIR``. Observation is passive: every hook calls the
original implementation and records what it returned. Challenge crops are written
to the diagnostic directory so a reviewer can verify the marks independently.
"""
import json
from pathlib import Path
import time
from urllib.parse import parse_qs


def _grid_area(grid):
    gx, gy, gw, gh = grid
    return (gx, gy, gx + gw, gy + gh)


def _save(image, path):
    try:
        image.convert("RGB").save(path)
        return True
    except Exception:
        return False


class Probe:
    def __init__(self, directory):
        self.root = Path(directory)
        self.attempt = 0
        self.round_dir = None
        self.state = {"attempts": [], "model_calls": [], "handoff": [], "pages": [],
                      "clicks": [], "clips": []}

    def directory(self, *parts):
        target = self.root.joinpath(*parts)
        target.mkdir(parents=True, exist_ok=True)
        return target

    def attempt_record(self):
        while len(self.state["attempts"]) < self.attempt:
            self.state["attempts"].append({"attempt": len(self.state["attempts"]) + 1, "rounds": []})
        return self.state["attempts"][self.attempt - 1]

    def flush(self):
        (self.root / "observations.json").write_text(
            json.dumps(self.state, indent=1, default=str), encoding="utf-8")

    def install(self):
        from captcha import hcaptcha, solver
        from captcha.interactions import DynamicInteractions
        from captcha.aria import AriaInteractions
        from auth import handoff
        from inss import http_login

        probe = self
        original_init = handoff.CaptchaHandoff.__init__
        original_solve_round = hcaptcha.solve_round
        original_solve = hcaptcha.solve
        original_prepare = DynamicInteractions.prepare
        original_wait = DynamicInteractions.wait_ready
        original_aria_wait = AriaInteractions.wait_ready
        original_click = hcaptcha.click
        original_clip = hcaptcha.modal_clip
        original_classify = handoff.classify_page
        original_intercept = handoff.CaptchaHandoff.intercept
        original_consume = handoff.CaptchaHandoff.consume

        def init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            probe.attempt += 1

        def solve_round(page, out_dir, rnd, *args, **kwargs):
            folder = probe.directory(f"attempt-{probe.attempt}", f"round-{rnd}")
            probe.round_dir = folder
            started = time.monotonic()
            done, info = original_solve_round(page, str(folder), rnd, *args, **kwargs)
            record = {"round": rnd, "done": bool(done), "elapsed_ms": round((time.monotonic() - started) * 1000),
                      "info": {key: value for key, value in info.items() if key != "model_usage"}}
            if info.get("model_usage"):
                record["model_usage"] = info["model_usage"]
            interactions = kwargs.get("interactions")
            if interactions is not None and interactions.metrics:
                record["selection"] = interactions.metrics[-1]
            probe.attempt_record().setdefault("rounds", []).append(record)
            probe.flush()
            return done, info

        def solve(header, tiles, model, *args, **kwargs):
            started = time.monotonic()
            usage = kwargs.get("usage")
            record = {"attempt": probe.attempt, "round_dir": str(probe.round_dir) if probe.round_dir else None,
                      "batch": bool(kwargs.get("batch")), "tiles": len(tiles),
                      "reference": kwargs.get("ref_img") is not None, "model": model}
            try:
                picks = original_solve(header, tiles, model, *args, **kwargs)
                record["picks"] = list(picks)
                return picks
            except Exception as error:
                record["error"] = f"{type(error).__name__}: {str(error)[:200]}"
                raise
            finally:
                record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
                if usage:
                    record["usage"] = dict(usage)
                probe.state["model_calls"].append(record)
                if probe.round_dir is not None:
                    _save(header, probe.round_dir / "model-header.png")
                    if kwargs.get("ref_img") is not None:
                        _save(kwargs["ref_img"], probe.round_dir / "model-reference.png")
                    if not kwargs.get("batch"):
                        for index, tile in enumerate(tiles, 1):
                            _save(tile, probe.round_dir / f"model-tile{index}.png")
                probe.flush()

        def prepare(self, before, grid, selected):
            original_prepare(self, before, grid, selected)
            probe.state["rounds_geometry"] = {"grid": list(grid), "size": list(before.size),
                                             "selected": sorted(selected)}
            if probe.round_dir is not None:
                try:
                    _save(before.crop(_grid_area(grid)), probe.round_dir / "grid-before.png")
                except Exception:
                    pass

        def wait_ready(self, page, clip):
            confirmed = original_wait(self, page, clip)
            probe._after_frame(page, clip, confirmed, "visual")
            return confirmed

        def aria_wait(self, page, clip):
            confirmed = original_aria_wait(self, page, clip)
            probe._after_frame(page, clip, confirmed, "aria")
            return confirmed

        def click(page, x, y):
            probe.state["clicks"].append({"attempt": probe.attempt, "x": round(x, 1), "y": round(y, 1),
                                          "round_dir": str(probe.round_dir) if probe.round_dir else None})
            return original_click(page, x, y)

        def modal_clip(page, box):
            result = original_clip(page, box)
            probe.state["clips"].append({"attempt": probe.attempt, **result})
            return result

        def classify_page(*args, **kwargs):
            result = original_classify(*args, **kwargs)
            probe.state["pages"].append({"attempt": probe.attempt, **result})
            return result

        def intercept(self, route):
            result = original_intercept(self, route)
            if not (getattr(self, "observed", False) or self.body is not None):
                return result
            fields = parse_qs(self.body or "", keep_blank_values=True)
            probe.state["handoff"].append({
                "attempt": probe.attempt, "native": bool(self.native),
                "observed": bool(self.observed), "captured": self.body is not None,
                "body_bytes": self.body_bytes, "field_names": list(self.field_names),
                "header_names": sorted(key.lower() for key in self.headers),
                "token_length": len(fields.get("h-captcha-response", [""])[0]),
            })
            probe.flush()
            return result

        def consume(self, session):
            started = time.monotonic()
            try:
                response = original_consume(self, session)
            except Exception as error:
                probe.state["handoff"].append({"attempt": probe.attempt, "error": type(error).__name__,
                                               "elapsed_ms": round((time.monotonic() - started) * 1000)})
                probe.flush()
                raise
            probe.state["handoff"].append({
                "attempt": probe.attempt, "post": True,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "status": response.status_code,
                "server": response.headers.get("Server", ""),
                "content_type": response.headers.get("Content-Type", ""),
                "body_bytes": len(response.content or b""),
            })
            probe.flush()
            return response

        handoff.CaptchaHandoff.__init__ = init
        handoff.CaptchaHandoff.intercept = intercept
        handoff.CaptchaHandoff.consume = consume
        handoff.classify_page = classify_page
        hcaptcha.solve_round = solve_round
        hcaptcha.solve = solve
        solver.solve = solve
        DynamicInteractions.prepare = prepare
        DynamicInteractions.wait_ready = wait_ready
        AriaInteractions.wait_ready = aria_wait
        hcaptcha.click = click
        hcaptcha.modal_clip = modal_clip
        # Worker logs stay visible in diagnostic runs instead of being discarded.
        http_login.redirect_stderr = _passthrough
        return self

    def _after_frame(self, page, clip, confirmed, backend):
        record = {"attempt": self.attempt, "backend": backend, "confirmed": bool(confirmed),
                  "round_dir": str(self.round_dir) if self.round_dir else None}
        if self.round_dir is not None:
            try:
                page.screenshot(path=str(Path(self.round_dir) / "grid-after.png"), clip=clip, timeout=4000)
            except Exception:
                record["after_frame"] = "unavailable"
        self.state.setdefault("selection_confirmations", []).append(record)
        self.flush()


class _passthrough:
    def __init__(self, target=None):
        self.target = target

    def __enter__(self):
        return self.target

    def __exit__(self, *error):
        return False


def install(directory):
    probe = Probe(directory).install()
    probe.flush()
    return probe
