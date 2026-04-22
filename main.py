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


def health_micro_plan(minutes: int, energy: int, r: random.Random) -> dict:
    minutes = int(clamp(minutes, 5, 240))
    energy = int(clamp(energy, 1, 10))

    hygiene = [
        "Brush + floss (2 minutes).",
        "Cold rinse on wrists (20 seconds).",
        "Moisturize + SPF (90 seconds).",
        "Lip balm + water sip (30 seconds).",
    ]
    movement_low = [
        "Neck rolls + shoulder openers (3 minutes).",
        "Slow walk, nasal breathing (8 minutes).",
        "Hip circles + calf raises (4 minutes).",
    ]
    movement_mid = [
        "Brisk walk with posture focus (10 minutes).",
        "3 rounds: 10 squats + 10 wall pushups.",
        "Stair intervals: 6 minutes total.",
    ]
    movement_high = [
        "12-minute tempo walk/jog mix.",
        "3 rounds: 12 lunges + 10 incline pushups + 20s plank.",
        "Jump rope or shadow boxing (8 minutes).",
    ]
    nutrition = [
        "Protein anchor: yogurt, eggs, tofu, or beans.",
        "Hydration: 400–600ml water in the next hour.",
        "Fiber: fruit + nuts or a small salad cup.",
        "Salt check: add a pinch of electrolytes if needed.",
    ]
    focus = [
        "Two deep breaths before you leave.",
        "Put one task on 'later' deliberately.",
        "Stand tall: ribs down, chin level.",
        "Text someone one honest sentence.",
    ]

    plan = {"hygiene": [], "movement": [], "nutrition": [], "focus": []}
    plan["hygiene"] = r.sample(hygiene, k=min(3, len(hygiene)))
    if energy <= 3:
        plan["movement"] = r.sample(movement_low, k=min(2, len(movement_low)))
    elif energy <= 7:
        plan["movement"] = r.sample(movement_mid, k=min(2, len(movement_mid)))
    else:
        plan["movement"] = r.sample(movement_high, k=min(2, len(movement_high)))
    plan["nutrition"] = r.sample(nutrition, k=min(3, len(nutrition)))
    plan["focus"] = r.sample(focus, k=min(2, len(focus)))

    # time budgeting
    base = 5 + (2 if energy >= 6 else 0)
    plan["timebox"] = {
        "minutes_available": minutes,
        "suggested": {
            "hygiene": min(6, max(3, base)),
            "movement": min(20, max(6, int(minutes * (0.18 + energy * 0.02)))),
            "nutrition": min(8, max(4, int(minutes * 0.12))),
            "focus": min(4, max(2, int(minutes * 0.05))),
        },
    }
    return plan


def style_script(occasion: str, venue: str, vibe: str, motif: str, accessory: str, fragrance: str, mantra: str) -> str:
    # Slightly poetic but actionable.
    parts = [
        f"Occasion: {occasion}. Venue vibe: {venue}.",
        f"Theme: {vibe} with a {motif} motif.",
        "Option A (subtle bloom): keep silhouette clean, put color in one detail.",
        "Option B (bold bloom): commit to contrast, echo it with one accessory.",
        f"Accessory: {accessory}.",
        f"Fragrance note: {fragrance}.",
        f"Mantra: {mantra}",
    ]
    return "\n".join(parts)


# -----------------------------
# Domain: signatures (local, for demo-quality "permits")
# -----------------------------


def sign_payload(secret_key: str, payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    mac = hmac.new(secret_key.encode("utf-8"), blob, hashlib.sha256).digest()
    return b64(mac)


def verify_payload(secret_key: str, payload: dict, signature: str) -> bool:
    try:
        expected = sign_payload(secret_key, payload)
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False


# -----------------------------
# App assembly
# -----------------------------


def ensure_seed_palettes(db: DB) -> None:
    rows = db.all("SELECT palette_id FROM palettes LIMIT 1")
    if rows:
        return
    now = iso_utc()
    seeds = [
        ("Neroli Night", ["#0b1020", "#19324a", "#2d5663", "#e3c6a6", "#f7efe6", "#c47a67", "#7a2f3b"], 211, True),
        ("Garden Gym", ["#081a13", "#19422f", "#5bbd8c", "#f3f0d7", "#e8a96a", "#b2453d"], 123, True),
        ("Lavender Pulse", ["#0c0c12", "#2a1a3a", "#4f2c6e", "#9d66cf", "#f3e9ff", "#f1c3dd", "#d85aa3", "#6e1d53"], 198, True),
        ("Street Asha Neon", ["#070910", "#142233", "#1de9b6", "#e6f0ff", "#ff5ca8", "#ffb703"], 164, True),
        ("Soft Tailor Bloom", ["#0f1115", "#273040", "#b5c7d3", "#f5f0ea", "#d1a7a2", "#8c3d3a"], 142, True),
    ]
    for name, colors, mood, active in seeds:
        pid = soft_uuid(f"{name}:{now}")[:18]
        db.exec(
            "INSERT INTO palettes(palette_id,name,colors_json,mood,created_at,active) VALUES(?,?,?,?,?,?)",
            (pid, name, json.dumps(colors, separators=(",", ":")), int(mood), now, 1 if active else 0),
        )


def mount_static(app: FastAPI, static_dir: str) -> None:
    if os.path.isdir(static_dir):
        app.mount("/streetofasha", StaticFiles(directory=static_dir, html=True), name="streetofasha")


def create_app(cfg: AppConfig) -> FastAPI:
    db = DB(cfg.db_path)
    migrate(db)
    ensure_seed_palettes(db)

    limiter = RateLimiter(cfg.request_budget_per_minute)

    app = FastAPI(title="YoFashion", version="1.0.0", docs_url="/docs", redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    mount_static(app, cfg.static_dir)

    @app.middleware("http")
    async def _rate_limit(request: Request, call_next):
        key = client_key(request)
        if request.url.path.startswith("/docs") or request.url.path.startswith("/openapi.json"):
            return await call_next(request)
        b = limiter.bucket_for(key)
        cost = 1.0
        if request.method.upper() in ("POST", "PUT", "DELETE"):
            cost = 1.5
        if not b.allow(cost=cost):
            return JSONResponse({"error": "rate_limited", "key": key}, status_code=429)
        resp = await call_next(request)
        resp.headers["X-YoFashion-Client"] = key
        return resp

    def require_session(request: Request) -> sqlite3.Row:
        sid = request.headers.get("x-yofashion-session") or request.query_params.get("session_id")
        if not sid:
            raise HTTPException(401, "Missing session (set x-yofashion-session header)")
        row = db.one("SELECT * FROM sessions WHERE session_id=?", (sid,))
        if row is None:
            raise HTTPException(401, "Unknown session")
        db.exec("UPDATE sessions SET last_seen=? WHERE session_id=?", (iso_utc(), sid))
        return row

    @app.get("/", response_class=HTMLResponse)
    async def home():
        html = f"""
        <html>
          <head>
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width,initial-scale=1"/>
            <title>YoFashion</title>
            <style>
              body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto; background: #0b1020; color: #f7efe6; margin: 0; padding: 40px; }}
              .card {{ max-width: 920px; margin: 0 auto; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10); border-radius: 16px; padding: 22px; }}
              a {{ color: #1de9b6; }}
              code {{ background: rgba(255,255,255,0.08); padding: 2px 6px; border-radius: 8px; }}
              .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
              @media (max-width: 780px) {{ .grid {{ grid-template-columns: 1fr; }} }}
            </style>
          </head>
          <body>
            <div class="card">
              <h1 style="margin-top: 0;">YoFashion</h1>
              <p>Local-first health + style assistant with floral design endpoints.</p>
              <div class="grid">
                <div>
                  <h3>Quickstart</h3>
                  <ol>
                    <li>Start a session via <code>POST /api/session/start</code></li>
                    <li>Save a profile via <code>PUT /api/profile</code></li>
                    <li>Ask for a plan via <code>POST /api/recommend</code></li>
                  </ol>
                </div>
                <div>
                  <h3>Street UI</h3>
                  <p>If you created the <code>streetofasha</code> folder beside YoFashion, open:</p>
                  <p><a href="/streetofasha/">/streetofasha/</a></p>
                </div>
              </div>
              <p style="opacity: 0.8;">Docs: <a href="/docs">/docs</a></p>
            </div>
          </body>
        </html>
        """
        return HTMLResponse(html)

    @app.get("/api/health")
    async def api_health():
        return {
            "ok": True,
            "ts": iso_utc(),
            "version": "1.0.0",
            "db": os.path.basename(cfg.db_path),
            "static": os.path.isdir(cfg.static_dir),
        }

    @app.post("/api/session/start", response_model=SessionOut)
    async def session_start(inp: SessionStartIn):
        created = iso_utc()
        seed = stable_hash_hex("YoFashion.session", inp.user_label, created, secrets.token_hex(8))
        sid = soft_uuid(seed)
        db.exec(
            "INSERT INTO sessions(session_id,created_at,user_label,locale,tz,last_seen) VALUES(?,?,?,?,?,?)",
            (sid, created, inp.user_label, inp.locale, inp.tz, created),
        )
        row = db.one("SELECT * FROM sessions WHERE session_id=?", (sid,))
        assert row is not None
        return SessionOut(**dict(row))

    @app.get("/api/session/me", response_model=SessionOut)
    async def session_me(sess=Depends(require_session)):
        return SessionOut(**dict(sess))

    @app.put("/api/profile")
    async def profile_put(inp: ProfileIn, sess=Depends(require_session)):
        now = iso_utc()
        db.exec(
            """
            INSERT INTO profiles(session_id,height_cm,style_vibe,skin_tone,activity_level,allergies,goals,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
              height_cm=excluded.height_cm,
              style_vibe=excluded.style_vibe,
              skin_tone=excluded.skin_tone,
              activity_level=excluded.activity_level,
              allergies=excluded.allergies,
              goals=excluded.goals,
              updated_at=excluded.updated_at
            """,
            (
                sess["session_id"],
                int(inp.height_cm),
                inp.style_vibe,
                inp.skin_tone,
                inp.activity_level,
                json.dumps(inp.allergies, separators=(",", ":")),
                json.dumps(inp.goals, separators=(",", ":")),
                now,
            ),
        )
        return {"ok": True, "updated_at": now}

    @app.get("/api/profile")
    async def profile_get(sess=Depends(require_session)):
        row = db.one("SELECT * FROM profiles WHERE session_id=?", (sess["session_id"],))
        if row is None:
            return {"exists": False}
        d = dict(row)
        d["exists"] = True
        d["allergies"] = json.loads(d["allergies"])
        d["goals"] = json.loads(d["goals"])
        return d

    @app.put("/api/wardrobe")
    async def wardrobe_put(inp: WardrobeIn, sess=Depends(require_session)):
        now = iso_utc()
        payload = [it.model_dump() for it in inp.items]
        db.exec(
            """
            INSERT INTO wardrobes(session_id,items_json,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
              items_json=excluded.items_json,
              updated_at=excluded.updated_at
            """,
            (sess["session_id"], json.dumps(payload, separators=(",", ":")), now),
        )
        return {"ok": True, "count": len(payload), "updated_at": now}

    @app.get("/api/wardrobe")
    async def wardrobe_get(sess=Depends(require_session)):
        row = db.one("SELECT items_json,updated_at FROM wardrobes WHERE session_id=?", (sess["session_id"],))
        if row is None:
            return {"items": [], "exists": False}
        return {"items": json.loads(row["items_json"]), "exists": True, "updated_at": row["updated_at"]}

    @app.post("/api/palettes", response_model=PaletteOut)
    async def palette_create(inp: PaletteIn, sess=Depends(require_session)):
        # session scoped create, but stored globally for simplicity.
        created = iso_utc()
        pid = soft_uuid(stable_hash_hex("YoFashion.palette", sess["session_id"], inp.name, created, secrets.token_hex(8)))[:18]
        colors = [normalize_hex(c) for c in inp.colors]
        db.exec(
            "INSERT INTO palettes(palette_id,name,colors_json,mood,created_at,active) VALUES(?,?,?,?,?,?)",
            (pid, inp.name, json.dumps(colors, separators=(",", ":")), int(inp.mood), created, 1 if inp.active else 0),
        )
        p = Palette(palette_id=pid, name=inp.name, colors=colors, mood=int(inp.mood), created_at=created, active=inp.active)
        return PaletteOut(
            palette_id=pid,
            name=p.name,
            colors=p.colors,
            mood=p.mood,
            created_at=p.created_at,
            active=p.active,
            suggested_text=palette_suggested_text(p),
        )

    @app.get("/api/palettes", response_model=list[PaletteOut])
    async def palettes_list(active: bool | None = None):
        if active is None:
            rows = db.all("SELECT * FROM palettes ORDER BY created_at DESC")
        else:
            rows = db.all("SELECT * FROM palettes WHERE active=? ORDER BY created_at DESC", (1 if active else 0,))
        out: list[PaletteOut] = []
        for row in rows:
            colors = json.loads(row["colors_json"])
            p = Palette(
                palette_id=row["palette_id"],
                name=row["name"],
                colors=colors,
                mood=int(row["mood"]),
                created_at=row["created_at"],
                active=bool(row["active"]),
            )
            out.append(
                PaletteOut(
                    palette_id=p.palette_id,
                    name=p.name,
                    colors=p.colors,
                    mood=p.mood,
                    created_at=p.created_at,
                    active=p.active,
                    suggested_text=palette_suggested_text(p),
                )
            )
        return out

    @app.post("/api/floral/svg")
    async def floral_svg_api(inp: FloralSVGIn):
        seed = inp.seed or stable_hash_hex("YoFashion.svg", iso_utc(), secrets.token_hex(8))
        svg = floral_svg(inp.palette, seed=seed, size=inp.size, petals=inp.petals, rings=inp.rings)
        return {
            "seed": seed,
            "svg": svg,
            "data_uri": "data:image/svg+xml;base64," + b64(svg.encode("utf-8")),
        }

    @app.post("/api/recommend")
    async def recommend(inp: RecommendIn, request: Request, sess=Depends(require_session)):
        # Get profile + wardrobe if present.
        prof = db.one("SELECT * FROM profiles WHERE session_id=?", (sess["session_id"],))
        ward = db.one("SELECT items_json FROM wardrobes WHERE session_id=?", (sess["session_id"],))

        profile = None
        if prof is not None:
            profile = dict(prof)
            profile["allergies"] = json.loads(profile["allergies"])
            profile["goals"] = json.loads(profile["goals"])

        items: list[WardrobeItem] = []
        if ward is not None:
            for it in json.loads(ward["items_json"]):
                try:
                    items.append(WardrobeItem(**it))
                except Exception:
                    continue

        # Seed randomness per request.
        seed = stable_hash_hex(
            "YoFashion.recommend",
            sess["session_id"],
            inp.occasion,
            inp.weather,
            inp.venue_vibe,
            inp.minutes_available,
            inp.energy,
            inp.accent,
            inp.palette_id,
            iso_utc(),
        )
        r = seeded_rng(seed)

        vibe = inp.venue_vibe.strip().lower()
        if vibe not in VIBES:
            vibe = pick(VIBES, r)

        motif = pick(MOTIFS, r)
        accessory = pick(ACCESSORIES, r)
        fragrance = pick(FRAGRANCE_NOTES, r)
        mantra = pick(MANTRAS, r)

        p = choose_palette(db, inp.palette_id, r)
        if not p.active:
            # Still allow but warn client.
            pass

        outfit = choose_outfit(items, inp.weather, inp.venue_vibe, vibe, r)
        micro = health_micro_plan(inp.minutes_available, inp.energy, r)

        # Generate floral preview.
        svg_seed = stable_hash_hex("YoFashion.svg.seed", seed, p.palette_id, motif)
        svg = floral_svg(p.colors, seed=svg_seed, size=768)

        notes = style_script(inp.occasion, inp.venue_vibe, vibe, motif, accessory, fragrance, mantra)

        # Persist as "look"
        look_id = soft_uuid(stable_hash_hex("YoFashion.look", sess["session_id"], seed, secrets.token_hex(8)))[:22]
        created = iso_utc()
        title = f"{vibe.title()} • {motif.title()} • {p.name}"
        outfit_json = json.dumps(outfit, separators=(",", ":"))
        db.exec(
            "INSERT INTO looks(look_id,session_id,title,notes,outfit_json,palette_id,floral_seed,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (look_id, sess["session_id"], title, notes, outfit_json, p.palette_id, svg_seed, created),
        )

        # Provide a local "permit-like" signature for UI integrity.
        permit = {
            "look_id": look_id,
            "session_id": sess["session_id"],
            "palette_id": p.palette_id,
            "floral_seed": svg_seed,
            "created_at": created,
            "title": title,
        }
        sig = sign_payload(cfg.secret_key, permit)

        return {
            "seed": seed,
            "look": {
                "look_id": look_id,
                "title": title,
                "notes": notes,
                "outfit": outfit,
                "palette": dataclasses.asdict(p),
                "floral": {
                    "seed": svg_seed,
                    "svg": svg,
                    "data_uri": "data:image/svg+xml;base64," + b64(svg.encode("utf-8")),
                },
                "health_micro": micro,
            },
            "profile_present": prof is not None,
            "wardrobe_items": len(items),
            "permit": {"payload": permit, "sig": sig},
        }

    @app.get("/api/looks", response_model=list[LookOut])
    async def looks_list(limit: int = 30, sess=Depends(require_session)):
        limit = int(clamp(limit, 1, 120))
        rows = db.all(
            "SELECT look_id,title,notes,outfit_json,palette_id,floral_seed,created_at FROM looks WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
            (sess["session_id"], limit),
        )
        out: list[LookOut] = []
        for row in rows:
            out.append(
                LookOut(
                    look_id=row["look_id"],
                    title=row["title"],
                    notes=row["notes"],
                    outfit=json.loads(row["outfit_json"]),
                    palette_id=row["palette_id"],
                    floral_seed=row["floral_seed"],
                    created_at=row["created_at"],
                )
            )
        return out

    @app.get("/api/looks/{look_id}", response_model=LookOut)
    async def look_get(look_id: str, sess=Depends(require_session)):
        row = db.one(
            "SELECT look_id,title,notes,outfit_json,palette_id,floral_seed,created_at FROM looks WHERE session_id=? AND look_id=?",
            (sess["session_id"], look_id),
        )
        if row is None:
            raise HTTPException(404, "look not found")
        return LookOut(
            look_id=row["look_id"],
            title=row["title"],
            notes=row["notes"],
            outfit=json.loads(row["outfit_json"]),
            palette_id=row["palette_id"],
            floral_seed=row["floral_seed"],
            created_at=row["created_at"],
        )

    @app.post("/api/permit/verify")
    async def permit_verify(payload: dict = Body(...), sig: str = Body(...)):
        ok = verify_payload(cfg.secret_key, payload, sig)
        return {"ok": ok, "payload": payload if ok else None}

    @app.get("/api/seed/demo")
    async def seed_demo():
        # Useful for UI demos.
        seed = stable_hash_hex("YoFashion.demo", iso_utc(), secrets.token_hex(12))
        r = seeded_rng(seed)
        return {
            "seed": seed,
            "motif": pick(MOTIFS, r),
            "vibe": pick(VIBES, r),
            "accessory": pick(ACCESSORIES, r),
            "fragrance": pick(FRAGRANCE_NOTES, r),
            "mantra": pick(MANTRAS, r),
        }

    @app.get("/api/debug/db")
    async def debug_db():
        # Exposes minimal counts, not rows.
        counts = {}
        for table in ["sessions", "profiles", "wardrobes", "palettes", "looks"]:
            row = db.one(f"SELECT COUNT(*) AS n FROM {table}")
            counts[table] = int(row["n"]) if row else 0
        return {"counts": counts, "ts": iso_utc()}

    return app


# -----------------------------
# CLI
# -----------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="YoFashion")
    p.add_argument("--serve", action="store_true", help="Run the web server")
    p.add_argument("--host", default=os.environ.get("YOFASHION_HOST", ""), help="Override host")
    p.add_argument("--port", default=os.environ.get("YOFASHION_PORT", ""), help="Override port")
    p.add_argument("--log", default=os.environ.get("YOFASHION_LOG", "INFO"), help="Log level")
    p.add_argument("--print-config", action="store_true", help="Print resolved config")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    _setup_logging(args.log)
    cfg = load_config()

    if args.host.strip():
        cfg = dataclasses.replace(cfg, host=args.host.strip())
    if args.port.strip():
        cfg = dataclasses.replace(cfg, port=safe_int(args.port.strip(), cfg.port))

    if args.print_config:
        print(json.dumps(dataclasses.asdict(cfg), indent=2, sort_keys=True))

    if not args.serve:
        print("YoFashion ready. Use --serve to start the API server.")
        return 0

    app = create_app(cfg)
    try:
        import uvicorn  # type: ignore
    except Exception as e:
        print("Missing dependency: uvicorn. Install requirements first.")
        print(str(e))
        return 2

    LOG.info("Starting YoFashion at http://%s:%s", cfg.host, cfg.port)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
