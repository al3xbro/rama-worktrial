"""The one place that talks to the model provider (OpenAI for now)."""
from __future__ import annotations

import base64
from functools import lru_cache
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel

from . import config  # noqa: F401  (loads .env)

T = TypeVar("T", bound=BaseModel)


@lru_cache(maxsize=1)
def client() -> OpenAI:
    return OpenAI()


def text_part(text: str) -> dict:
    return {"type": "input_text", "text": text}


def image_part(data: bytes, mime: str) -> dict:
    return {"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(data).decode()}", "detail": "high"}


def parse(model: str, instructions: str, content: list[dict], schema: type[T]) -> T:
    """One structured-output call. The result is validated against `schema` by the SDK."""
    resp = client().responses.parse(
        model=model,
        instructions=instructions,
        input=[{"role": "user", "content": content}],
        text_format=schema,
    )
    if resp.output_parsed is None:
        raise RuntimeError(f"model returned no parsed output (status={resp.status})")
    return resp.output_parsed
