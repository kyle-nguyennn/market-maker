"""Exact JSON numbers at the venue boundary; never round through float."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


def encode_json(value: Any) -> bytes:
    def encode(item: Any) -> str:
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("non-finite Decimal is not a JSON number")
            return str(item)
        if item is None or isinstance(item, (str, bool, int)):
            return json.dumps(item, ensure_ascii=True, allow_nan=False)
        if isinstance(item, float):
            raise TypeError("use Decimal instead of float at the venue boundary")
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(v) for v in item) + "]"
        if isinstance(item, dict):
            if not all(isinstance(k, str) for k in item):
                raise TypeError("JSON object keys must be strings")
            return "{" + ",".join(json.dumps(k) + ":" + encode(v)
                                  for k, v in item.items()) + "}"
        raise TypeError("unsupported JSON value")

    return encode(value).encode("utf-8")


def decode_json(body: bytes) -> dict[str, Any]:
    def invalid_constant(_: str) -> None:
        raise ValueError("non-finite JSON number")

    result = json.loads(body, parse_float=Decimal, parse_constant=invalid_constant)
    if not isinstance(result, dict):
        raise ValueError("expected a JSON object")
    return result
