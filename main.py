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
