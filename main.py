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
        u = u / 255.0
        return u / 12.92 if u <= 0.04045 else ((u + 0.055) / 1.055) ** 2.4

    r, g, b = hex_to_rgb(c)
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast_ratio(a: str, b: str) -> float:
    la = relative_luminance(a)
    lb = relative_luminance(b)
    l1, l2 = (la, lb) if la >= lb else (lb, la)
    return (l1 + 0.05) / (l2 + 0.05)


def best_text_color(bg: str) -> str:
    # Choose between near-black and near-white.
    dark = "#0b0d11"
    light = "#f7f8fb"
    return light if contrast_ratio(bg, light) >= contrast_ratio(bg, dark) else dark


# -----------------------------
# Config
# -----------------------------


@dataclass(frozen=True)
class AppConfig:
    host: str
    port: int
    db_path: str
    data_dir: str
    static_dir: str
    secret_key: str
    allow_origins: list[str]
    request_budget_per_minute: int


def load_config() -> AppConfig:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, ".data")
    static_dir = os.path.join(base_dir, "..", "streetofasha")
    os.makedirs(data_dir, exist_ok=True)
    secret_key = os.environ.get("YOFASHION_SECRET") or secrets.token_hex(32)

    allow = os.environ.get("YOFASHION_CORS", "http://127.0.0.1:8899,http://localhost:8899").split(",")
    allow_origins = [a.strip() for a in allow if a.strip()]

    return AppConfig(
        host=os.environ.get("YOFASHION_HOST", "127.0.0.1"),
        port=safe_int(os.environ.get("YOFASHION_PORT", "8899"), 8899),
        db_path=os.environ.get("YOFASHION_DB", os.path.join(data_dir, "yofashion.sqlite3")),
        data_dir=data_dir,
        static_dir=static_dir,
        secret_key=secret_key,
        allow_origins=allow_origins,
        request_budget_per_minute=safe_int(os.environ.get("YOFASHION_RPM", "240"), 240),
    )


# -----------------------------
# Database
# -----------------------------


class DB:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA foreign_keys=ON;")
            setattr(self._local, "conn", c)
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
