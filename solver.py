"""OpenRouter vision solver: header crop + 9 tiles -> tile indices to click.
Reads OPENROUTER_API_KEY from env. Model passed via --model (user benchmarks).
"""
import base64
import io
import json
import os
import re

import requests
from PIL import Image

API = "https://openrouter.ai/api/v1/chat/completions"


def _b64(img: Image.Image, scale=2) -> str:
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


PROMPT = (
    "You solve an hCaptcha image challenge (Brazilian Portuguese). "
    "Image 0 is the task header (instruction + reference item). "
    "Images 1-9 are the 3x3 grid tiles in row-major order (1=top-left, 9=bottom-right). "
    "Read the instruction, decide which tiles match, and reply with ONLY a JSON array "
    "of tile numbers (1-9), e.g. [1,5,6,7]. No other text."
)


PROMPT_REF = (
    "You solve an hCaptcha image challenge (Brazilian Portuguese). "
    "Image 0 is the task instruction text. "
    "Image 1 is the REFERENCE item named/shown by the instruction. "
    "Images 2-10 are the 3x3 grid tiles in row-major order (2=top-left, 10=bottom-right). "
    "Select tiles matching the instruction relative to the reference item, and reply "
    "with ONLY a JSON array of tile numbers (2-10 mapped back to 1-9 grid positions), "
    "e.g. [1,5,6,7]. No other text."
)


def solve(header_img: Image.Image, tiles, model: str, timeout=90, ref_img=None,
          reasoning_effort=None) -> list[int]:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    if ref_img is None:
        content = [{"type": "text", "text": PROMPT},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(header_img)}},
                   {"type": "text", "text": "Tiles 1-9:"}]
    else:
        content = [{"type": "text", "text": PROMPT_REF},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(header_img)}},
                   {"type": "text", "text": "Reference item:"},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(ref_img)}},
                   {"type": "text", "text": "Tiles (numbered 1-9 row-major in reply):"}]
    for t in tiles:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(t)}})
    body = {"model": model, "messages": [{"role": "user", "content": content}]}
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    r = requests.post(API, headers={"Authorization": f"Bearer {key}",
                                     "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\[[\d,\s]*\]", text)
    if not m:
        raise ValueError(f"no JSON array in model reply: {text[:200]}")
    idx = [int(x) for x in json.loads(m.group(0)) if 1 <= int(x) <= 9]
    return sorted(set(idx))
