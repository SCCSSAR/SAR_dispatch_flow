"""Tests for backend/recap_map.py — the monthly recap's drawn map pictures.

PUBLIC repo: every coordinate and title here is invented. No network: tiles come from a fake fetcher.
"""

import io
from pathlib import Path

import pytest
from PIL import Image

import recap_map as rm

TILE_RGB = (200, 230, 200)
EXTENT = [[37.50, -122.20], [36.90, -121.20]]


def _tile_bytes(rgb=TILE_RGB):
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), rgb).save(buf, "PNG")
    return buf.getvalue()


def _colours(im):
    return {c for _, c in im.getcolors(im.width * im.height)}


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def __call__(self, z, x, y):
        self.calls.append((z, x, y))
        return _tile_bytes()


def _marker(sym, title, lat, lng, cls="Marker", description="Zorblat lives here, DOB 1/2/41"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {"class": cls, "marker-symbol": sym, "title": title, "description": description}}


FEATURES = [
    {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-121.9, 37.3], [-121.8, 37.4]]},
     "properties": {"class": "Shape", "title": "0.5mi"}},
    _marker("point", "Glove", 37.301, -121.901),
    _marker("point", "2. Example Park, Oakvale", 37.302, -121.902),
    _marker("placemark2", "LKP — 12 Quillback St, Oakvale", 37.303, -121.903),
    _marker("hut", "Residence — 12 Quillback St, Oakvale", 37.304, -121.904),
    _marker("cp", "1. 40 Example Ave, Oakvale", 37.305, -121.905),
    _marker("point", "Subject Found", 37.310, -121.910),
]


class TestSelectMarkers:
    def test_roles(self):
        got = rm.select_markers(FEATURES)
        # Staging alternates are never drawn, only the command post (presenter, 2026-11-10).
        assert [m.role for m in got] == ["lkp", "residence", "cp", "find"]
        assert got[0].title == "LKP — 12 Quillback St, Oakvale"
        assert (got[3].lat, got[3].lng) == (37.310, -121.910)

    def test_description_never_carried(self):
        for m in rm.select_markers(FEATURES):
            assert "Zorblat" not in repr(m)

    def test_mp_location_is_find(self):
        assert rm.select_markers([_marker("point", "MP Location", 37.3, -121.9)])[0].role == "find"


class TestWorldPx:
    def test_origin(self):
        assert rm.lnglat_to_world_px(0, 0, 0) == pytest.approx((128, 128))

    def test_east_is_right_north_is_up(self):
        x1, y1 = rm.lnglat_to_world_px(-121.9, 37.3, 10)
        x2, y2 = rm.lnglat_to_world_px(-121.8, 37.4, 10)
        assert x2 > x1 and y2 < y1


class TestChooseZoom:
    def test_close_points_zoom_in(self):
        pts = [(37.300, -121.900), (37.309, -121.900)]  # about 1 km apart
        assert rm.choose_zoom(pts, 1200, 900, 3, 17) >= 14

    def test_county_zooms_out(self):
        assert rm.choose_zoom([tuple(p) for p in EXTENT], 1200, 900, 3, 17) <= 10

    def test_single_point_uses_max(self):
        assert rm.choose_zoom([(37.3, -121.9)], 1200, 900, 3, 15) == 15


class TestRenderMap:
    def test_draws_and_credits(self, tmp_path):
        fetch = FakeFetcher()
        out = rm.render_map(rm.select_markers(FEATURES), 1200, 900, fetch, tmp_path / "m.png")
        im = Image.open(out).convert("RGB")
        assert im.size == (1200, 900)
        corner = im.crop((900, 860, 1200, 900))
        assert _colours(corner) - {TILE_RGB}, "attribution not drawn"
        z = fetch.calls[0][0]
        assert all(c[0] == z for c in fetch.calls)
        assert len(fetch.calls) <= 6 * 5  # a 1200x900 frame straddles at most 6 x 5 tiles

    def test_markers_drawn(self, tmp_path):
        out = rm.render_map(rm.select_markers(FEATURES), 1200, 900, FakeFetcher(), tmp_path / "m.png")
        im = Image.open(out).convert("RGB")
        # Each drawn role's fill colour is present; tiles and the credit alone supply none of them.
        assert {rm._ROLE_COLOUR[r] for r in ("lkp", "residence", "cp", "find")} <= _colours(im)

    def test_extent_widens_frame(self, tmp_path):
        fetch_a, fetch_b = FakeFetcher(), FakeFetcher()
        ms = rm.select_markers(FEATURES)
        rm.render_map(ms, 1200, 900, fetch_a, tmp_path / "a.png")
        rm.render_map(ms, 1200, 900, fetch_b, tmp_path / "b.png", extent=[tuple(p) for p in EXTENT], min_zoom=7)
        assert fetch_b.calls[0][0] < fetch_a.calls[0][0]


class TestRenderMapNothingToDraw:
    def test_raises(self, tmp_path):
        with pytest.raises(ValueError, match="nothing to draw"):
            rm.render_map([], 1200, 900, FakeFetcher(), tmp_path / "m.png")


class TestCachedFetcher:
    def test_second_call_uses_cache(self, tmp_path):
        hits = []

        def opener(url, headers):
            hits.append((url, headers["User-Agent"]))
            return _tile_bytes()

        fetch = rm.cached_osm_fetcher(tmp_path, opener=opener)
        a = fetch(10, 163, 396)
        b = fetch(10, 163, 396)
        assert a == b and len(hits) == 1
        assert hits[0][0] == "https://tile.openstreetmap.org/10/163/396.png"
        assert hits[0][1] == rm.USER_AGENT
        assert (tmp_path / "10" / "163" / "396.png").exists()


class TestOverview:
    def test_cancelled_pin_is_grey(self, tmp_path):
        pins = [(1, 37.30, -121.90, False), (2, 37.10, -121.60, True)]
        out = rm.render_overview(pins, [tuple(p) for p in EXTENT], FakeFetcher(), tmp_path / "o.png")
        im = Image.open(out).convert("RGB")
        assert im.size == (1000, 1000)
        colours = _colours(im)
        assert rm.PIN_ORANGE in colours and rm.PIN_GREY in colours

    def test_county_fills_the_frame(self, tmp_path):
        # A 15% margin left a county 1 deg wide at zoom 9 (whole Bay Area); the overview wants zoom 10.
        fetch = FakeFetcher()
        rm.render_overview([], [(37.484, -122.202), (36.896, -121.208)], fetch, tmp_path / "o.png")
        assert fetch.calls[0][0] == 10

    def test_no_pins_still_draws_county(self, tmp_path):
        out = rm.render_overview([], [tuple(p) for p in EXTENT], FakeFetcher(), tmp_path / "o.png")
        assert Image.open(out).size == (1000, 1000)


class TestMilesBetween:
    def test_sf_to_la(self):
        assert rm.miles_between((37.7749, -122.4194), (34.0522, -118.2437)) == pytest.approx(347, abs=2)


class TestLabelText:
    def test_dash_drawable(self):
        # PIL's built-in font has no em dash glyph; it drew as a box on the first real render.
        assert rm.label_text("Residence — 12 Quillback St, Oakvale") == "Residence - 12 Quillback St, Oakvale"

    def test_long_title_cut(self):
        assert rm.label_text("1. " + "x" * 80).endswith("…") is False
        assert len(rm.label_text("1. " + "x" * 80)) <= rm.LABEL_MAX


class TestPlaceLabels:
    def _overlap(self, a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

    def test_same_point_labels_do_not_overlap(self):
        boxes = rm.place_labels([(500, 400, 300, 24), (500, 400, 280, 24), (502, 401, 250, 24)], 1200, 900)
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                assert not self._overlap(boxes[i], boxes[j])

    def test_first_label_stays_beside_its_marker(self):
        box = rm.place_labels([(500, 400, 300, 24)], 1200, 900)[0]
        assert box[0] > 500 and box[1] <= 400 <= box[3]

    def test_label_kept_inside_frame(self):
        box = rm.place_labels([(1150, 880, 300, 24)], 1200, 900)[0]
        assert box[2] <= 1200 and box[3] <= 900 and box[0] >= 0


class TestSpreadPins:
    def test_overlapping_pins_separated(self):
        out = rm.spread_pins([(500, 500), (505, 502), (500, 500), (900, 100)], 16)
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                (x1, y1), (x2, y2) = out[i], out[j]
                assert ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 >= 2 * 16
        assert out[0] == (500, 500) and out[3] == (900, 100)  # first and far pins stay put

    def test_spread_stays_near(self):
        out = rm.spread_pins([(500, 500), (500, 500)], 16)
        assert ((out[1][0] - 500) ** 2 + (out[1][1] - 500) ** 2) ** 0.5 <= 6 * 16
