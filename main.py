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
            try:
                c.close()
            finally:
                setattr(self._local, "conn", None)

    def exec(self, sql: str, params: tuple = ()) -> None:
        c = self.conn()
        c.execute(sql, params)
        c.commit()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        cur = self.conn().execute(sql, params)
        row = cur.fetchone()
        return row

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        cur = self.conn().execute(sql, params)
        return list(cur.fetchall())

    def tx(self):
        c = self.conn()
        return c


def migrate(db: DB) -> None:
    c = db.conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta(
          k TEXT PRIMARY KEY,
          v TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions(
          session_id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          user_label TEXT NOT NULL,
          locale TEXT NOT NULL,
          tz TEXT NOT NULL,
          last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS profiles(
          session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
          height_cm INTEGER NOT NULL,
          style_vibe TEXT NOT NULL,
          skin_tone TEXT NOT NULL,
          activity_level TEXT NOT NULL,
          allergies TEXT NOT NULL,
          goals TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wardrobes(
          session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
          items_json TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS palettes(
          palette_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          colors_json TEXT NOT NULL,
          mood INTEGER NOT NULL,
          created_at TEXT NOT NULL,
          active INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS looks(
          look_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
          title TEXT NOT NULL,
          notes TEXT NOT NULL,
          outfit_json TEXT NOT NULL,
          palette_id TEXT NOT NULL REFERENCES palettes(palette_id) ON DELETE RESTRICT,
          floral_seed TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_looks_session_created ON looks(session_id, created_at);
        """
    )
    c.commit()

    row = db.one("SELECT v FROM meta WHERE k='schema_version'")
    if row is None:
        db.exec("INSERT INTO meta(k,v) VALUES('schema_version', '1')")


# -----------------------------
# Models
# -----------------------------


class SessionStartIn(BaseModel):
    user_label: str = Field(min_length=1, max_length=64)
    locale: str = Field(default="en-US", min_length=2, max_length=32)
    tz: str = Field(default="UTC", min_length=3, max_length=48)

    @field_validator("user_label")
    @classmethod
    def _clean_label(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Empty user_label")
        return v[:64]


class SessionOut(BaseModel):
    session_id: str
    created_at: str
    user_label: str
    locale: str
    tz: str
    last_seen: str


class ProfileIn(BaseModel):
    height_cm: int = Field(ge=120, le=230)
    style_vibe: str = Field(min_length=2, max_length=64)
    skin_tone: str = Field(min_length=2, max_length=48)
    activity_level: str = Field(min_length=2, max_length=32)
    allergies: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)

    @field_validator("allergies", "goals")
    @classmethod
    def _cap_list(cls, v: list[str]) -> list[str]:
        out = []
        for x in v[:24]:
            x = x.strip()
            if x:
                out.append(x[:64])
        return out


class WardrobeItem(BaseModel):
    kind: str = Field(min_length=2, max_length=32)
    label: str = Field(min_length=1, max_length=80)
    color: str = Field(default="#222222")
    warmth: int = Field(default=0, ge=-2, le=3)  # -2 very light, 3 very warm
    formality: int = Field(default=0, ge=-2, le=3)
    tags: list[str] = Field(default_factory=list)

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        return normalize_hex(v)

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for t_ in v[:16]:
            t_ = t_.strip()
            if t_:
                out.append(t_[:32])
        return out


class WardrobeIn(BaseModel):
    items: list[WardrobeItem] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def _items(cls, v: list[WardrobeItem]) -> list[WardrobeItem]:
        return v[:250]


class PaletteIn(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    colors: list[str] = Field(min_length=3, max_length=12)
    mood: int = Field(default=128, ge=0, le=255)
    active: bool = True

    @field_validator("colors")
    @classmethod
    def _colors(cls, v: list[str]) -> list[str]:
        out = []
        for c in v:
            out.append(normalize_hex(c))
        return out


class PaletteOut(BaseModel):
    palette_id: str
    name: str
    colors: list[str]
    mood: int
    created_at: str
    active: bool
    suggested_text: str


class RecommendIn(BaseModel):
    occasion: str = Field(min_length=2, max_length=80)
    weather: str = Field(default="mild", min_length=2, max_length=40)
    minutes_available: int = Field(default=20, ge=5, le=240)
    energy: int = Field(default=5, ge=1, le=10)
    venue_vibe: str = Field(default="street", min_length=2, max_length=48)
    accent: str | None = Field(default=None)
    palette_id: str | None = Field(default=None)

    @field_validator("accent")
    @classmethod
    def _accent(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if len(v) > 48:
            return v[:48]
        return v


class FloralSVGIn(BaseModel):
    palette: list[str] = Field(min_length=3, max_length=12)
    seed: str | None = None
    size: int = Field(default=1024, ge=256, le=2048)
    petals: int | None = Field(default=None, ge=3, le=42)
    rings: int | None = Field(default=None, ge=2, le=18)

    @field_validator("palette")
    @classmethod
    def _palette(cls, v: list[str]) -> list[str]:
        return [normalize_hex(x) for x in v]


class LookOut(BaseModel):
    look_id: str
    title: str
    notes: str
    outfit: dict
    palette_id: str
    floral_seed: str
    created_at: str


# -----------------------------
# Rate limiting (simple + local)
# -----------------------------


class TokenBucket:
    def __init__(self, rate_per_minute: int, burst: int | None = None):
        self.rate_per_second = max(1.0, rate_per_minute / 60.0)
        self.capacity = float(burst if burst is not None else max(10, int(rate_per_minute * 0.35)))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def allow(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        with self.lock:
            dt = now - self.updated
            self.updated = now
            self.tokens = min(self.capacity, self.tokens + dt * self.rate_per_second)
            if self.tokens >= cost:
                self.tokens -= cost
                return True
            return False


class RateLimiter:
    def __init__(self, rpm: int):
        self.rpm = rpm
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def bucket_for(self, key: str) -> TokenBucket:
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = TokenBucket(self.rpm)
                self._buckets[key] = b
            return b


def client_key(req: Request) -> str:
    ip = req.client.host if req.client else "unknown"
    ua = req.headers.get("user-agent", "na")
    return stable_hash_hex("YoFashion.client", ip, ua)[:24]


# -----------------------------
# Domain: palettes + floral generator
# -----------------------------


@dataclass(frozen=True)
class Palette:
    palette_id: str
    name: str
    colors: list[str]
    mood: int
    created_at: str
    active: bool

    @property
    def bg(self) -> str:
        return self.colors[0]

    @property
    def accent(self) -> str:
        return self.colors[-1]

    @property
    def mid(self) -> str:
        return self.colors[len(self.colors) // 2]


def palette_suggested_text(p: Palette) -> str:
    return best_text_color(p.bg)


def seeded_rng(seed: str) -> random.Random:
    raw = stable_hash_bytes("YoFashion.rng", seed)
    n = int.from_bytes(raw[:8], "big", signed=False)
    return random.Random(n)


def choose_palette(db: DB, palette_id: str | None, r: random.Random) -> Palette:
    if palette_id:
        row = db.one("SELECT * FROM palettes WHERE palette_id=?", (palette_id,))
        if row is None:
            raise HTTPException(404, "Unknown palette_id")
        return Palette(
            palette_id=row["palette_id"],
            name=row["name"],
            colors=json.loads(row["colors_json"]),
            mood=int(row["mood"]),
            created_at=row["created_at"],
            active=bool(row["active"]),
        )
    rows = db.all("SELECT * FROM palettes WHERE active=1 ORDER BY created_at DESC")
    if not rows:
        rows = db.all("SELECT * FROM palettes ORDER BY created_at DESC")
    if not rows:
        raise HTTPException(500, "No palettes available")
    row = rows[r.randrange(0, len(rows))]
    return Palette(
        palette_id=row["palette_id"],
        name=row["name"],
        colors=json.loads(row["colors_json"]),
        mood=int(row["mood"]),
        created_at=row["created_at"],
        active=bool(row["active"]),
    )


def _polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def floral_svg(palette: list[str], seed: str, size: int = 1024, petals: int | None = None, rings: int | None = None) -> str:
    palette = [normalize_hex(c) for c in palette]
    r = seeded_rng(seed)
    s = float(size)
    cx = s / 2.0
    cy = s / 2.0

    bg = palette[0]
    mid = palette[len(palette) // 2]
    accent = palette[-1]

    petals_n = petals if petals is not None else (6 + r.randrange(0, 18))
    rings_n = rings if rings is not None else (3 + r.randrange(0, 12))
    petals_n = int(clamp(petals_n, 3, 42))
    rings_n = int(clamp(rings_n, 2, 18))

    tilt = r.random() * 360.0
    base_radius = s * (0.28 + r.random() * 0.18)
    petal_len = s * (0.28 + r.random() * 0.22)
    bend = s * (0.05 + r.random() * 0.07)

    def fmt(x: float) -> str:
        return f"{x:.3f}".rstrip("0").rstrip(".")

    # Background gradient and subtle grain dots.
    grain_n = 18 + r.randrange(0, 92)
    grain = []
    for i in range(grain_n):
        gx = r.random() * s
        gy = r.random() * s
        gr = 0.7 + r.random() * 2.2
        go = 0.07 + r.random() * 0.09
        grain.append(
            f"<circle cx='{fmt(gx)}' cy='{fmt(gy)}' r='{fmt(gr)}' fill='#ffffff' opacity='{fmt(go)}'/>"
        )

    # Ring circles.
    rings_svg = []
    for i in range(rings_n):
        rr = (i + 1) / rings_n
        rad = s * (0.07 + rr * 0.38)
        col = palette[i % len(palette)]
        w = 1.0 + (i % 3) * 0.9
        op = 0.10 + (i / (rings_n + 3)) * 0.42
        rings_svg.append(
            f"<circle cx='{fmt(cx)}' cy='{fmt(cy)}' r='{fmt(rad)}' fill='none' stroke='{col}' stroke-width='{fmt(w)}' opacity='{fmt(op)}'/>"
        )

    # Petal path for one petal.
    top = _polar(cx, cy, base_radius + petal_len, tilt)
    left = _polar(cx, cy, base_radius + petal_len * 0.55, tilt - 26.0)
    right = _polar(cx, cy, base_radius + petal_len * 0.55, tilt + 26.0)
    inner = _polar(cx, cy, base_radius * 0.55, tilt)

    c1 = (left[0] + bend, left[1] - bend)
    c2 = (right[0] - bend, right[1] - bend)

    petal_d = (
        f"M {fmt(inner[0])} {fmt(inner[1])} "
        f"C {fmt(c1[0])} {fmt(c1[1])} {fmt(left[0])} {fmt(left[1])} {fmt(top[0])} {fmt(top[1])} "
        f"C {fmt(right[0])} {fmt(right[1])} {fmt(c2[0])} {fmt(c2[1])} {fmt(inner[0])} {fmt(inner[1])} Z"
    )

    # Replicate petals by rotation.
    petal_svg = []
    for i in range(petals_n):
        ang = tilt + i * (360.0 / petals_n)
        fill = blend(accent, mid, (i % 7) / 7.0)
        op = 0.14 + (r.random() * 0.22)
        petal_svg.append(
            f"<g transform='rotate({fmt(ang)} {fmt(cx)} {fmt(cy)})'>"
            f"<path d='{petal_d}' fill='{fill}' opacity='{fmt(op)}'/>"
            f"<path d='{petal_d}' fill='none' stroke='{mid}' stroke-width='{fmt(1.6)}' opacity='{fmt(0.35)}'/>"
            f"</g>"
        )

    # Title stamp.
    stamp = (
        f"<g font-family='ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto' fill='{mid}' opacity='0.86'>"
        f"<text x='{fmt(s*0.05)}' y='{fmt(s*0.09)}' font-size='{fmt(s*0.035)}'>YoFashion</text>"
        f"<text x='{fmt(s*0.05)}' y='{fmt(s*0.125)}' font-size='{fmt(s*0.018)}'>seed {seed[:10]}</text>"
        f"</g>"
    )

    svg = (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{size}' height='{size}' viewBox='0 0 {size} {size}'>"
        f"<defs>"
        f"<radialGradient id='bg' cx='50%' cy='42%' r='75%'>"
        f"<stop offset='0%' stop-color='{mid}' stop-opacity='0.92'/>"
        f"<stop offset='70%' stop-color='{bg}' stop-opacity='0.98'/>"
        f"<stop offset='100%' stop-color='{accent}' stop-opacity='1'/>"
        f"</radialGradient>"
        f"<filter id='blur' x='-20%' y='-20%' width='140%' height='140%'>"
        f"<feGaussianBlur stdDeviation='{fmt(s*0.0045)}'/>"
        f"</filter>"
        f"</defs>"
        f"<rect width='{size}' height='{size}' fill='url(#bg)'/>"
        f"<g opacity='0.95' filter='url(#blur)'>"
        + "".join(rings_svg)
        + "</g>"
        f"<g opacity='0.96'>"
        + "".join(petal_svg)
        + "</g>"
        f"<g opacity='0.9'>"
        + "".join(grain)
        + "</g>"
        + stamp
        + "</svg>"
    )
    return svg


# -----------------------------
# Domain: health + style planning
# -----------------------------


VIBES = [
    "street",
    "minimal",
    "artsy",
    "sport-luxe",
    "soft-tailored",
    "techwear",
    "vintage",
    "clean-girl",
    "night-bloom",
    "garden-gym",
]

MOTIFS = ["rose", "iris", "dahlia", "jasmine", "hibiscus", "camellia", "lotus", "orchid", "peony"]

ACCESSORIES = [
    "thin silver chain",
    "soft leather tote",
    "structured mini bag",
    "sport watch",
    "oversized sunglasses",
    "scarf tied to bag",
    "minimal hoops",
    "pearl stud set",
    "canvas cap",
]

FRAGRANCE_NOTES = [
    "neroli + cedar",
    "rosewater + musk",
    "bergamot + vetiver",
    "jasmine tea + amber",
    "iris + clean linen",
    "salted citrus + sage",
    "fig leaf + sandalwood",
]

MANTRAS = [
    "Breathe deep, dress bright, move kindly today.",
    "Soft petals, strong steps, steady heart, go.",
    "Nourish first, sparkle second, then bloom louder.",
    "Small rituals, big glow, let it happen.",
    "Warm core, calm mind, crisp fit, onward.",
    "Walk steady, sip water, bloom in silence.",
]


def wardrobe_summary(items: list[WardrobeItem]) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for it in items:
        k = it.kind.lower()
        kinds[k] = kinds.get(k, 0) + 1
    return dict(sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))


def _score_item(it: WardrobeItem, weather: str, venue: str, vibe: str) -> float:
    s = 0.0
    w = weather.lower()
    if "cold" in w:
        s += it.warmth * 0.9
    elif "hot" in w:
        s -= it.warmth * 0.7
    elif "rain" in w:
        if any(t_ in it.tags for t_ in ["waterproof", "shell", "boots"]):
            s += 1.5
    if venue.lower() in it.tags:
        s += 1.25
    if vibe.lower() in it.tags:
        s += 0.9
    if it.formality > 1 and "street" in venue.lower():
        s -= 0.35
    return s


def choose_outfit(items: list[WardrobeItem], weather: str, venue: str, vibe: str, r: random.Random) -> dict:
    if not items:
        # fallback "virtual capsule"
        capsule = [
            WardrobeItem(kind="top", label="ribbed tee", color="#111111", tags=["street", "minimal"]),
            WardrobeItem(kind="bottom", label="straight-leg denim", color="#1c2b3a", tags=["street", "vintage"]),
            WardrobeItem(kind="outer", label="light bomber", color="#2b2f36", warmth=1, tags=["street", "techwear"]),
            WardrobeItem(kind="shoes", label="clean sneakers", color="#f0f2f5", tags=["street", "sport-luxe"]),
        ]
        items = capsule

    by_kind: dict[str, list[WardrobeItem]] = {}
    for it in items:
        by_kind.setdefault(it.kind.lower(), []).append(it)

    def pick_best(kind: str, want: int = 1) -> list[WardrobeItem]:
        opts = by_kind.get(kind, [])
        if not opts:
            return []
        scored = [(it, _score_item(it, weather, venue, vibe) + (r.random() * 0.12)) for it in opts]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [it for it, _ in scored[:want]]

    outfit_items: list[WardrobeItem] = []
    outfit_items += pick_best("top", 1)
    outfit_items += pick_best("bottom", 1)
    outfit_items += pick_best("outer", 1) if "cold" in weather.lower() or r.random() < 0.45 else []
    outfit_items += pick_best("shoes", 1)

    # if still sparse, add "accessory" placeholder using the palette.
    return {
        "items": [it.model_dump() for it in outfit_items],
        "coverage": wardrobe_summary(outfit_items),
    }
