"""Optional 18-image prefetch: passive browser responses, pixel mapping, RAM only."""
from collections import Counter
import hashlib
import io
import time
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageStat

from . import solver
from .interactions import DynamicInteractions

MAX_BODY_BYTES = 2_000_000
MAX_IMAGES = 36
MAX_PIXELS = 4_000_000


def captcha_url(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return (parts.scheme == "https" and parts.port in (None, 443)
                and not parts.username and not parts.password and any(
                    host == root or host.endswith("." + root) for root in ("hcaptcha.com", "hcaptcha.net")))
    except ValueError:
        return False


def fingerprint(image):
    return hashlib.sha256(str(image.size).encode() + image.tobytes()).hexdigest()


def match_tiles(tiles, candidates):
    """Conservatively associate rendered tiles with original assets; ties fall back."""
    if len(tiles) != 9:
        return None
    def normalize(image):
        return image.convert("RGB").resize((32, 32)).crop((4, 4, 28, 28))
    images = {fingerprint(image): image for image in candidates}
    normalized = {key: normalize(image) for key, image in images.items()}
    matches = []
    for tile in tiles:
        rendered = normalize(tile)
        ranked = sorted((sum(ImageStat.Stat(ImageChops.difference(rendered, image)).mean) / 3, key)
                        for key, image in normalized.items())
        if (len(ranked) < 2 or ranked[0][0] > 40
                or ranked[0][0] / max(.001, ranked[1][0]) > .9):
            return None
        matches.append(ranked[0][1])
    if len(set(matches)) != 9:
        return None
    return [images[key] for key in matches]


def same_image(before, after, size):
    if before is None or after is None:
        return before is after
    before = before.convert("RGB").resize(size)
    after = after.convert("RGB").resize(size)
    return sum(ImageStat.Stat(ImageChops.difference(before, after)).mean) / 3 <= 3


class PrefetchedSolver:
    def __init__(self, session, model_solver=None):
        self.session = session  # Dedicated vision transport; never the authenticated SSO session.
        self.model_solver = model_solver or solver.solve
        self.images = {}
        self.pending = {}
        self.predictions = {}
        self.generation = 0
        self.header = self.reference = None
        self.page = None
        self.pixels = 0
        self.first_ready_at = None
        self.last_image_at = 0
        self.disabled = False
        self.counts = Counter()
        self.fallback_reasons = Counter()

    def attach(self, page):
        self.page = page
        page.on("request", self.observe_request)
        page.on("response", self.observe_response)

    def observe_request(self, request):
        if not captcha_url(request.url):
            return
        if urlsplit(request.url).path.startswith("/getcaptcha"):
            self.generation += 1
            self.images.clear()
            self.predictions.clear()
            self.header = self.reference = None
            self.pixels = 0
            self.first_ready_at = None
            self.last_image_at = 0
            self.disabled = False
            self.counts["challenge_requests"] += 1
        if len(self.pending) < 256:
            self.pending[request] = self.generation

    def observe_response(self, response):
        generation = self.pending.pop(response.request, None)
        if generation != self.generation or not captcha_url(response.url):
            return
        try:
            mime = response.headers.get("content-type", "").split(";")[0]
            if response.status != 200 or not mime.startswith("image/") or mime == "image/svg+xml":
                return
            if int(response.headers.get("content-length", "0")) > MAX_BODY_BYTES:
                return
            raw = response.body()
            if len(raw) > MAX_BODY_BYTES:
                return
            with Image.open(io.BytesIO(raw)) as source:
                if not (32 <= source.width <= 512 and 32 <= source.height <= 512):
                    return
                image = source.convert("RGB")
            # body() can dispatch more events, including a new challenge request.
            if generation != self.generation:
                return
            key = fingerprint(image)
            if key in self.images:
                return
            pixels = image.width * image.height
            if len(self.images) >= MAX_IMAGES or self.pixels + pixels > MAX_PIXELS:
                return
            self.images[key] = image
            self.pixels += pixels
            self.last_image_at = time.monotonic()
            self.counts["decoded_images"] += 1
        except Exception:
            # A canceled or unsupported response must not interrupt the login.
            self.counts["ignored_responses"] += 1

    def images_ready(self, crop, grid):
        if not DynamicInteractions.images_ready(crop, grid):
            return False
        now = time.monotonic()
        if self.first_ready_at is None:
            self.first_ready_at = now
        # Give already-requested assets a bounded opportunity to finish.
        return now - self.first_ready_at >= 1.5 or now - self.last_image_at >= .3

    def _fallback(self, reason, header, tiles, model, kwargs):
        self.fallback_reasons[reason] += 1
        self.counts["fallback_calls"] += 1
        self.counts["model_calls"] += 1
        return self.model_solver(header, tiles, model, session=self.session, **kwargs)

    def __call__(self, header, tiles, model, *, ref_img=None, **kwargs):
        kwargs["ref_img"] = ref_img
        generation = self.generation
        matched = match_tiles(tiles, self.images.values())
        if self.disabled or matched is None or len({image.size for image in matched}) != 1:
            return self._fallback("mapping_unavailable", header, tiles, model, kwargs)
        if self.header is not None and (not same_image(self.header, header, (128, 64))
                or not same_image(self.reference, ref_img, (128, 128))):
            self.predictions.clear()
            self.disabled = True
            return self._fallback("instruction_changed", header, tiles, model, kwargs)
        keys = [fingerprint(image) for image in matched]
        if not all(key in self.predictions for key in keys):
            candidates = {key: image for key, image in self.images.items() if image.size == matched[0].size}
            if len(candidates) != 18:
                return self._fallback("pool_incomplete", header, tiles, model, kwargs)
            order = sorted(candidates)
            self.counts["model_calls"] += 1
            self.counts["batch_calls"] += 1
            selected = self.model_solver(header, [candidates[key] for key in order], model,
                                         batch=True, session=self.session, **kwargs)
            if self.page is not None:
                self.page.wait_for_timeout(0)
            if generation != self.generation:
                raise RuntimeError("captcha_images_changed")
            self.predictions = {key: index + 1 in selected for index, key in enumerate(order)}
            self.header = header.copy()
            self.reference = ref_img.copy() if ref_img is not None else None
        else:
            self.counts["cache_hits"] += 1
            usage = kwargs.get("usage")
            if usage is not None:
                usage.clear()
        return [index + 1 for index, key in enumerate(keys) if self.predictions[key]]

    def statistics(self):
        names = ("challenge_requests", "decoded_images", "ignored_responses", "model_calls",
                 "batch_calls", "fallback_calls", "cache_hits")
        return {"enabled": True, **{name: self.counts[name] for name in names},
                "fallback_reasons": dict(self.fallback_reasons)}

    def close(self):
        if self.page is not None:
            self.page.remove_listener("request", self.observe_request)
            self.page.remove_listener("response", self.observe_response)
            self.page = None
        self.images.clear()
        self.pending.clear()
        self.predictions.clear()
        self.header = self.reference = None
