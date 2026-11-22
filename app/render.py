"""AI renderings of a new concrete driveway / sidewalk.

The customer uploads a photo of their driveway (or we use the aerial crop of
their lot) and picks a finish; an image-editing model repaints the concrete.

Provider is pluggable. Out of the box it supports OpenAI's image edit API:
    RENDER_PROVIDER=openai  OPENAI_API_KEY=...  [OPENAI_IMAGE_MODEL=gpt-image-1]
Add another provider by implementing a function with the same signature as
``_openai`` and registering it in ``PROVIDERS``.
"""
from __future__ import annotations

import base64
import io
import os

import httpx
from PIL import Image

FINISH_PROMPTS = {
    "broom": "a brand-new light-grey broom-finished concrete driveway with clean saw-cut control joints",
    "exposed": "a brand-new exposed-aggregate concrete driveway with a smooth trowelled border",
    "stamped": "a brand-new stamped concrete driveway in an ashlar slate pattern, warm sandstone colour with a darker charcoal border band",
    "stained": "a brand-new smooth concrete driveway with a rich walnut-brown acid stain and sealed satin sheen",
}


class RenderUnavailable(RuntimeError):
    pass


def build_prompt(finish: str, extra: str = "", aerial: bool = False) -> str:
    surface = FINISH_PROMPTS.get(finish, FINISH_PROMPTS["broom"])
    view = "This is a top-down aerial photo." if aerial else "This is a photo of a house and its driveway."
    return (
        f"{view} Replace only the existing driveway and front walk surfaces with {surface}. "
        "Keep the house, lawn, landscaping, vehicles, camera angle, lighting and shadows exactly the same. "
        "Photorealistic, natural daylight, realistic scale and texture. " + (extra or "")
    ).strip()


def _prep_png(img_bytes: bytes, max_side: int = 1024) -> bytes:
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _openai(image_png: bytes, prompt: str) -> bytes:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RenderUnavailable("Set OPENAI_API_KEY to enable AI renderings.")
    model = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
    r = httpx.post(
        "https://api.openai.com/v1/images/edits",
        headers={"Authorization": f"Bearer {key}"},
        data={"model": model, "prompt": prompt, "size": "auto", "n": "1"},
        files={"image": ("driveway.png", image_png, "image/png")},
        timeout=180,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"render provider error {r.status_code}: {r.text[:300]}")
    item = r.json()["data"][0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    return httpx.get(item["url"], timeout=60).content


PROVIDERS = {"openai": _openai}


def available() -> bool:
    prov = os.environ.get("RENDER_PROVIDER", "openai")
    return prov == "openai" and bool(os.environ.get("OPENAI_API_KEY"))


def render(image_bytes: bytes, finish: str, extra: str = "", aerial: bool = False) -> bytes:
    prov = os.environ.get("RENDER_PROVIDER", "openai")
    fn = PROVIDERS.get(prov)
    if not fn:
        raise RenderUnavailable(f"Unknown RENDER_PROVIDER '{prov}'.")
    return fn(_prep_png(image_bytes), build_prompt(finish, extra, aerial))
