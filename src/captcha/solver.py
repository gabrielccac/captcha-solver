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
          reasoning_effort=None, usage=None, api_key=None, *, batch=False,
          session=None) -> list[int]:
    key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    if batch:
        content = [{"type": "text", "text": (
            f"Read the hCaptcha task instruction in the first image. Then classify ALL {len(tiles)} "
            "candidate images against that SAME instruction. Return ONLY a JSON array of matching "
            "candidate numbers. Candidates are explicitly numbered below; do not number the "
            "instruction or reference image.")},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(header_img)}}]
        if ref_img is not None:
            content.extend([{"type": "text", "text": "Reference item for the instruction:"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(ref_img)}}])
        for index, tile in enumerate(tiles, 1):
            content.extend([{"type": "text", "text": f"Candidate {index}:"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(tile)}}])
    elif ref_img is None:
        content = [{"type": "text", "text": PROMPT},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(header_img)}},
                   {"type": "text", "text": "Tiles 1-9:"}]
    else:
        content = [{"type": "text", "text": PROMPT_REF},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(header_img)}},
                   {"type": "text", "text": "Reference item:"},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(ref_img)}},
                   {"type": "text", "text": "Tiles (numbered 1-9 row-major in reply):"}]
    if not batch:
        for t in tiles:
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + _b64(t)}})
    # Avoid reserving credits for the provider's 65k-token output default.
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "max_tokens": 4096, "usage": {"include": True}}
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    transport = session if session is not None else requests
    r = transport.post(API, headers={"Authorization": f"Bearer {key}",
                                     "Content-Type": "application/json"},
                      json=body, timeout=timeout)
    try:
        r.raise_for_status()
        result = r.json()
    finally:
        r.close()
    if usage is not None:
        usage.update(result.get("usage") or {})
    text = result["choices"][0]["message"]["content"]
    m = re.search(r"\[[\d,\s]*\]", text)
    if not m:
        raise ValueError(f"no JSON array in model reply: {text[:200]}")
    values = json.loads(m.group(0))
    if batch and any(type(value) is not int or not 1 <= value <= len(tiles) for value in values):
        raise ValueError("invalid_batch_indices")
    idx = [int(x) for x in values if 1 <= int(x) <= (len(tiles) if batch else 9)]
    return sorted(set(idx))
