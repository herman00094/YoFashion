"""
YoFashion — a local-first health + style assistant with floral design capability.

Run:
  python YoFashion.py --serve

API base:
  http://127.0.0.1:8899

This module intentionally stays "single-file" for portability.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as _dt
import hashlib
import hmac
import json
import logging
import math
import os
import random
import re
import secrets
import sqlite3
import string
import textwrap
import threading
import time
import typing as t
import uuid
from dataclasses import dataclass

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator


# -----------------------------
# Logging
# -----------------------------

LOG = logging.getLogger("YoFashion")


def _setup_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


# -----------------------------
# Utilities
# -----------------------------


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.timezone.utc)


def iso_utc(dt: _dt.datetime | None = None) -> str:
    return (dt or utc_now()).isoformat().replace("+00:00", "Z")


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def safe_int(x: t.Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def stable_hash_bytes(*parts: t.Any) -> bytes:
    h = hashlib.blake2b(digest_size=32)
    for p in parts:
        if isinstance(p, bytes):
            h.update(p)
        elif isinstance(p, str):
            h.update(p.encode("utf-8"))
        else:
            h.update(json.dumps(p, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        h.update(b"\x1f")
    return h.digest()


def stable_hash_hex(*parts: t.Any) -> str:
    return stable_hash_bytes(*parts).hex()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def pick(seq: list[str], r: random.Random) -> str:
    return seq[r.randrange(0, len(seq))]


def soft_uuid(seed: str) -> str:
    # Generates a stable UUID for repeatable seeds; not suitable for security.
    d = stable_hash_bytes("YoFashion.soft_uuid", seed)
    return str(uuid.UUID(bytes=d[:16], version=4))


def redact(s: str, keep: int = 4) -> str:
    if len(s) <= keep * 2:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep * 2) + s[-keep:]


HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def is_hex_color(c: str) -> bool:
    return bool(HEX_COLOR_RE.match(c))


def normalize_hex(c: str) -> str:
    c = c.strip()
    if not is_hex_color(c):
        raise ValueError(f"Not a hex color: {c}")
    return "#" + c[1:].lower()


def hex_to_rgb(c: str) -> tuple[int, int, int]:
    c = normalize_hex(c)
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"


def lerp(a: float, b: float, t_: float) -> float:
    return a + (b - a) * t_


def blend(c1: str, c2: str, t_: float) -> str:
    r1, g1, b1 = hex_to_rgb(c1)
    r2, g2, b2 = hex_to_rgb(c2)
    return rgb_to_hex(
        (
            int(round(lerp(r1, r2, t_))),
            int(round(lerp(g1, g2, t_))),
            int(round(lerp(b1, b2, t_))),
        )
    )


def relative_luminance(c: str) -> float:
    # WCAG luminance
    def f(u: float) -> float:
