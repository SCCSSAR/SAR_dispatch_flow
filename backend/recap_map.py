"""Map pictures for the monthly incident recap, drawn from CalTopo map data over OpenStreetMap tiles.

CalTopo offers no image of a Secret map (spike 2026-11-10: every export path 404s like an invented
one, and the /pv/ preview 500s for Secret maps whether signed or not), and changing sharing was
rejected. So the recap draws its own picture: only the markers that tell the story (LKP, residence,
staging, find), titles as CalTopo shows them, descriptions never. No shapes, tracks or range rings.

OSM tile policy: identifying User-Agent, local cache, no prefetch, credit on every image.
"""

from __future__ import annotations

import ast
import io
import math
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

USER_AGENT = "SAR-monthly-recap/1.0 (local, low volume; https://github.com/SCCSSAR/SAR_dispatch_flow)"
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
ATTRIBUTION = "© OpenStreetMap contributors"
TILE = 256

PIN_ORANGE = (241, 89, 42)
PIN_GREY = (154, 154, 154)
_ROLE_COLOUR = {"lkp": (20, 20, 20), "residence": (110, 60, 160), "cp": (210, 30, 30),
                "staging": (30, 90, 200), "find": PIN_ORANGE}

FIND_TITLE_RE = re.compile(r"\b(subject|mp)\s+(found|location)\b", re.I)
_STAGING_TITLE_RE = re.compile(r"^\d+\.\s")


def _caltopo_symbols() -> dict[str, str]:
    src = Path(__file__).with_name("caltopo.py").read_text()
    return {t.id: n.value.value for n in ast.parse(src).body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name) and t.id.startswith("SYMBOL_")
            and isinstance(n.value, ast.Constant)}


@dataclass
class Marker:
    role: str
    title: str
    lat: float
    lng: float


def select_markers(features: list[dict]) -> list[Marker]:
    sym = _caltopo_symbols()
    by_symbol = {sym["SYMBOL_LKP"]: "lkp", sym["SYMBOL_RESIDENCE"]: "residence",
                 sym["SYMBOL_STAGING_OFFICER"]: "cp"}
    out = []
    for f in features:
        p = f.get("properties") or {}
        if p.get("class") != "Marker" or not (f.get("geometry") or {}).get("coordinates"):
            continue
        title = p.get("title") or ""
        symbol = p.get("marker-symbol")
        if FIND_TITLE_RE.search(title):
            role = "find"
        elif symbol in by_symbol:
            role = by_symbol[symbol]
        elif symbol == sym["SYMBOL_STAGING_ALT"] and _STAGING_TITLE_RE.match(title):
            role = "staging"
        else:
            continue
        lng, lat = f["geometry"]["coordinates"][:2]
        out.append(Marker(role=role, title=title, lat=float(lat), lng=float(lng)))
    return out


# ── tiles ────────────────────────────────────────────────────────────────────

TileFetcher = Callable[[int, int, int], bytes]


def _urllib_open(url: str, headers: dict) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def cached_osm_fetcher(cache_dir: Path, opener: Callable[[str, dict], bytes] | None = None) -> TileFetcher:
    opener = opener or _urllib_open

    def fetch(z: int, x: int, y: int) -> bytes:
        path = cache_dir / str(z) / str(x) / f"{y}.png"
        if path.exists():
            return path.read_bytes()
        data = opener(TILE_URL.format(z=z, x=x, y=y), {"User-Agent": USER_AGENT})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return data

    return fetch


def lnglat_to_world_px(lng: float, lat: float, zoom: int) -> tuple[float, float]:
    scale = TILE * 2 ** zoom
    x = (lng + 180.0) / 360.0 * scale
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def choose_zoom(points: list[tuple[float, float]], width: int, height: int,
                min_zoom: int, max_zoom: int, margin: float = 0.15) -> int:
    for z in range(max_zoom, min_zoom - 1, -1):
        px = [lnglat_to_world_px(lng, lat, z) for lat, lng in points]
        xs, ys = [p[0] for p in px], [p[1] for p in px]
        if max(xs) - min(xs) <= width * (1 - 2 * margin) and max(ys) - min(ys) <= height * (1 - 2 * margin):
            return z
    return min_zoom


def miles_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lat1, lng1), (lat2, lng2) = a, b
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lng2 - lng1)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))


# ── drawing ──────────────────────────────────────────────────────────────────

def _font(size: int):
    return ImageFont.load_default(size=size)


def _base(points: list[tuple[float, float]], width: int, height: int, fetch: TileFetcher,
          min_zoom: int, max_zoom: int):
    z = choose_zoom(points, width, height, min_zoom, max_zoom)
    px = [lnglat_to_world_px(lng, lat, z) for lat, lng in points]
    cx = (min(p[0] for p in px) + max(p[0] for p in px)) / 2
    cy = (min(p[1] for p in px) + max(p[1] for p in px)) / 2
    ox, oy = cx - width / 2, cy - height / 2
    img = Image.new("RGB", (width, height), (238, 238, 238))
    n = 2 ** z
    for ty in range(math.floor(oy / TILE), math.floor((oy + height - 1) / TILE) + 1):
        if not 0 <= ty < n:
            continue
        for tx in range(math.floor(ox / TILE), math.floor((ox + width - 1) / TILE) + 1):
            tile = Image.open(io.BytesIO(fetch(z, tx % n, ty))).convert("RGB")
            img.paste(tile, (round(tx * TILE - ox), round(ty * TILE - oy)))

    def to_px(lat: float, lng: float) -> tuple[float, float]:
        x, y = lnglat_to_world_px(lng, lat, z)
        return x - ox, y - oy

    return img, to_px


def _credit(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    f = _font(14)
    w = d.textlength(ATTRIBUTION, font=f)
    x0, y0 = img.width - w - 12, img.height - 24
    d.rectangle((x0 - 4, y0 - 2, img.width, img.height), fill=(255, 255, 255))
    d.text((x0, y0), ATTRIBUTION, fill=(60, 60, 60), font=f)


LABEL_MAX = 48
_LABEL_GAP = 17


def label_text(title: str) -> str:
    """A marker title as the built-in font can draw it: plain dashes, cut to LABEL_MAX."""
    text = title.replace("—", "-").replace("–", "-")
    return text if len(text) <= LABEL_MAX else text[:LABEL_MAX - 3].rstrip() + "..."


def place_labels(anchors: list[tuple[float, float, float, float]], width: int,
                 height: int) -> list[tuple[float, float, float, float]]:
    """Label boxes (x0, y0, x1, y1) for markers at (x, y) with label size (w, h); no two overlap."""
    placed: list[tuple[float, float, float, float]] = []

    def clash(b):
        return any(b[0] < o[2] and o[0] < b[2] and b[1] < o[3] and o[1] < b[3] for o in placed)

    for x, y, w, h in anchors:
        x0 = x + _LABEL_GAP if x + _LABEL_GAP + w <= width else x - _LABEL_GAP - w
        x0 = max(0.0, x0)
        y0 = min(max(0.0, y - h / 2), height - h)
        box = (x0, y0, x0 + w, y0 + h)
        step = h + 4
        tries = 0
        while clash(box) and tries < 200:
            tries += 1
            k = (tries + 1) // 2 * (1 if tries % 2 else -1)
            ny = min(max(0.0, y - h / 2 + k * step), height - h)
            box = (x0, ny, x0 + w, ny + h)
        placed.append(box)
    return placed


def _draw_marker(d: ImageDraw.ImageDraw, m: Marker, x: float, y: float) -> None:
    c = _ROLE_COLOUR[m.role]
    r = 11
    if m.role == "residence":
        d.rectangle((x - r, y - r, x + r, y + r), fill=c, outline=(255, 255, 255), width=2)
    elif m.role == "cp":
        d.polygon([(x, y - r - 3), (x - r, y + r), (x + r, y + r)], fill=c, outline=(255, 255, 255))
    elif m.role == "find":
        pts = []
        for i in range(10):
            rad = (r + 5) if i % 2 == 0 else (r - 3)
            a = math.pi / 2 + i * math.pi / 5
            pts.append((x + rad * math.cos(a), y - rad * math.sin(a)))
        d.polygon(pts, fill=c, outline=(255, 255, 255))
    else:
        d.ellipse((x - r, y - r, x + r, y + r), fill=c, outline=(255, 255, 255), width=2)


def _draw_labels(d: ImageDraw.ImageDraw, items: list[tuple[str, float, float]], width: int, height: int) -> None:
    f = _font(18)
    texts = [(label_text(t), x, y) for t, x, y in items if t]
    boxes = place_labels([(x, y, d.textlength(t, font=f) + 8, 26) for t, x, y in texts], width, height)
    for (t, x, y), (x0, y0, x1, y1) in zip(texts, boxes):
        if not (y0 <= y <= y1):
            d.line((x, y, x0 if x0 > x else x1, (y0 + y1) / 2), fill=(60, 60, 60), width=2)
        d.rectangle((x0, y0, x1, y1), fill=(255, 255, 255), outline=(170, 170, 170))
        d.text((x0 + 4, y0 + 3), t, fill=(20, 20, 20), font=f)


def render_map(markers: list[Marker], width: int, height: int, fetch: TileFetcher, out: Path, *,
               extent: list[tuple[float, float]] | None = None, min_zoom: int = 11,
               max_zoom: int = 17) -> Path:
    if not markers:
        raise ValueError("nothing to draw")
    points = [(m.lat, m.lng) for m in markers] + list(extent or [])
    img, to_px = _base(points, width, height, fetch, min_zoom, max_zoom)
    d = ImageDraw.Draw(img)
    ordered = sorted(markers, key=lambda m: m.role == "find")  # the find on top
    for m in ordered:
        _draw_marker(d, m, *to_px(m.lat, m.lng))
    _draw_labels(d, [(m.title, *to_px(m.lat, m.lng)) for m in ordered], width, height)
    _credit(img)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out


def render_overview(pins: list[tuple[int, float, float, bool]], county_extent: list[tuple[float, float]],
                    fetch: TileFetcher, out: Path) -> Path:
    """Numbered in-county pins (number, lat, lng, cancelled) over the whole county."""
    points = [(lat, lng) for _, lat, lng, _ in pins] + list(county_extent)
    img, to_px = _base(points, 1000, 1000, fetch, 3, 12)
    d = ImageDraw.Draw(img)
    f = _font(18)
    for number, lat, lng, cancelled in pins:
        x, y = to_px(lat, lng)
        r = 16
        d.ellipse((x - r, y - r, x + r, y + r), fill=PIN_GREY if cancelled else PIN_ORANGE)
        label = str(number)
        d.text((x - d.textlength(label, font=f) / 2, y - 10), label, fill=(255, 255, 255), font=f)
    _credit(img)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "PNG")
    return out
