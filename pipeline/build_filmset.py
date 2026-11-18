#!/usr/bin/env python3
"""Build our own stylised 3D model of Monte-Carlo, like an animated-film set.

Nothing here comes from Google: every shape is derived from open data and
every colour is authored by us.

  ground     IGN LiDAR HD terrain model (MNT, 50 cm), meshed at 3 m and
             draped with a painted map: OpenStreetMap roads, pavements,
             squares, gardens, pools, plus soft shade around buildings.
  buildings  IGN BD TOPO outlines. Heights from the LiDAR surface model
             (MNS): each building is split into up to three stepped volumes
             (podium, body, tower) like an architect's massing model. Tiled
             roofs (seen as terracotta in the IGN photo) get hipped roofs;
             walls get a Riviera colour and a style (Belle Époque with
             shutters, or modern glass) that the viewer's shader turns into
             windows and floors.
  trees      Found in the LiDAR (tall, not a building), drawn as low-poly
             broadleaf trees, umbrella pines and cypresses.
  harbour    Yachts found in the LiDAR over water become stylised hulls;
             OpenStreetMap piers become pontoons.
  sea        One flat plane the viewer animates.

Output: viewer/filmset/monte-carlo.glb (y-up glTF in an east-north-up frame
centred on Casino square, heights in metres above the ellipsoid minus the
geoid offset handled by the viewer) and viewer/filmset/monte-carlo.json.

Inputs (downloaded by --fetch into data/raw/filmset/):
  mnt.tif, mns.tif   IGN LiDAR HD 50 cm via Géoplateforme WMS
  ortho.jpg          IGN orthophoto 50 cm (only used to classify roofs/trees)
  bdtopo.json        IGN BD TOPO building outlines
  osm.xml            OpenStreetMap extract (api.openstreetmap.org /map)

Usage:
  python pipeline/build_filmset.py --fetch
  python pipeline/build_filmset.py
"""
import argparse
import hashlib
import io
import json
import math
import struct
import subprocess
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pyproj
import shapely
import triangle
from PIL import Image, ImageDraw, ImageFilter
from scipy import ndimage
from shapely import contains_xy
from shapely.geometry import LineString, MultiPolygon, Polygon, box, shape
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "filmset"
OUT = ROOT / "viewer" / "filmset"

# District: Monte-Carlo, Casino square, the north side of Port Hercule and
# the Larvotto edge. Lambert-93 metres.
X0, Y0, X1, Y1 = 1056000, 6302800, 1057200, 6304000
RES = 0.5
ORIGIN = (7.4280, 43.7395)          # Casino square: the model's 0,0
GEOID_N = 48.9                      # same as viewer/main.js
GRID = 3.0                          # ground mesh spacing, m
TEX = 4096                          # ground map size, px (street-level sharpness)
SEA_FLOOR = -4.0

WMS = "https://data.geopf.fr/wms-r"


# ---------------------------------------------------------------- fetch ----
WD_QUERY = """SELECT ?item ?itemLabel ?coord ?inc ?styleLabel ?floors WHERE {
  SERVICE wikibase:around { ?item wdt:P625 ?coord . bd:serviceParam wikibase:center "Point(7.4282 43.7393)"^^geo:wktLiteral .
    bd:serviceParam wikibase:radius "0.9" . }
  OPTIONAL { ?item wdt:P571 ?inc } OPTIONAL { ?item wdt:P149 ?style } OPTIONAL { ?item wdt:P1101 ?floors }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,fr". } }"""


def fetch():
    RAW.mkdir(parents=True, exist_ok=True)
    b = f"{X0},{Y0},{X1},{Y1}"
    n = int((X1 - X0) / RES)
    lid = ("{w}?SERVICE=WMS&VERSION=1.3.0&EXCEPTIONS=text/xml&REQUEST=GetMap&LAYERS=IGNF_LIDAR-HD_{k}"
           "_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93&FORMAT=image/geotiff&STYLES=&CRS=EPSG:2154"
           "&BBOX={b}&WIDTH={n}&HEIGHT={n}")
    lon0, lat0 = to_lonlat(np.array([X0 - 50]), np.array([Y0 - 50]))
    lon1, lat1 = to_lonlat(np.array([X1 + 50]), np.array([Y1 + 50]))
    urls = {
        "mnt.tif": lid.format(w=WMS, k="MNT", b=b, n=n),
        "mns.tif": lid.format(w=WMS, k="MNS", b=b, n=n),
        "ortho.jpg": (f"{WMS}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ORTHOIMAGERY.ORTHOPHOTOS"
                      f"&STYLES=&FORMAT=image/jpeg&CRS=EPSG:2154&BBOX={b}&WIDTH={n}&HEIGHT={n}"),
        "bdtopo.json": ("https://data.geopf.fr/wfs/ows?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
                        "&TYPENAMES=BDTOPO_V3:batiment&OUTPUTFORMAT=application/json&SRSNAME=EPSG:2154"
                        f"&COUNT=5000&BBOX={b},EPSG:2154"),
        "osm.xml": ("https://api.openstreetmap.org/api/0.6/map?bbox="
                    f"{lon0[0]:.4f},{lat0[0]:.4f},{lon1[0]:.4f},{lat1[0]:.4f}"),
        # Dated places around the square (year built, style, floors).
        "wikidata.json": "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(WD_QUERY),
    }
    for name, url in urls.items():
        out = RAW / name
        if out.exists() and out.stat().st_size > 1000:
            continue
        print("fetch", name)
        req = urllib.request.Request(url, headers={"User-Agent": "MC-3D film set builder"})
        out.write_bytes(urllib.request.urlopen(req, timeout=300).read())
        time.sleep(1.2)


# ----------------------------------------------------------- coordinates ----
_TO_LL = pyproj.Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
_TO_ECEF = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)


def to_lonlat(x, y):
    return _TO_LL.transform(x, y)


def _frame():
    lon, lat = map(math.radians, ORIGIN)
    o = np.array(_TO_ECEF.transform(ORIGIN[0], ORIGIN[1], 0.0))
    e = np.array([-math.sin(lon), math.cos(lon), 0.0])
    n = np.array([-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)])
    u = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    return np.vstack([e, n, u]), o


ROT, ORG = _frame()


def to_enu(x, y, h):
    """Lambert-93 + NGF height -> east, north, up (m) around Casino square.
    'up' is above the ellipsoid, as the viewer places the origin at height 0."""
    x, y, h = np.broadcast_arrays(np.asarray(x, float), np.asarray(y, float), np.asarray(h, float))
    lon, lat = _TO_LL.transform(x.ravel(), y.ravel())
    ex, ey, ez = _TO_ECEF.transform(lon, lat, h.ravel() + GEOID_N)
    p = np.column_stack([ex, ey, ez]) - ORG
    return (p @ ROT.T).astype(np.float64)


# Small district: a 2D affine fit of Lambert-93 -> east/north is exact to a
# few millimetres and much faster for millions of texture/vertex lookups.
def _affine():
    xs, ys = np.meshgrid(np.linspace(X0, X1, 5), np.linspace(Y0, Y1, 5))
    enu = to_enu(xs.ravel(), ys.ravel(), np.zeros(xs.size))
    A = np.column_stack([xs.ravel() - X0, ys.ravel() - Y0, np.ones(xs.size)])
    coef, *_ = np.linalg.lstsq(A, enu, rcond=None)
    return coef


AFF = _affine()


def enu_fast(x, y, h):
    x, y, h = np.broadcast_arrays(np.asarray(x, float), np.asarray(y, float), np.asarray(h, float))
    A = np.column_stack([x.ravel() - X0, y.ravel() - Y0, np.ones(x.size)])
    p = A @ AFF
    p[:, 2] += h.ravel()   # the fit holds the geoid offset and earth curvature at h = 0
    return p


# ---------------------------------------------------------------- rasters ----
class Raster:
    def __init__(self, a):
        self.a = a

    def sample(self, x, y):
        c = np.floor((np.asarray(x) - X0) / RES).astype(int)
        r = np.floor((Y1 - np.asarray(y)) / RES).astype(int)
        ok = (c >= 0) & (c < self.a.shape[1]) & (r >= 0) & (r < self.a.shape[0])
        out = np.full(np.shape(x), np.nan, np.float32)
        out[ok] = self.a[r[ok], c[ok]]
        return out


def load_raster(name):
    import tifffile
    a = tifffile.imread(RAW / name).astype(np.float32)
    a[a < -100] = np.nan
    return a


def cell_xy(r, c):
    return X0 + (c + 0.5) * RES, Y1 - (r + 0.5) * RES


# -------------------------------------------------------------------- OSM ----
def load_osm():
    root = ET.parse(RAW / "osm.xml").getroot()
    nodes, ways, points = {}, [], []
    for n in root.findall("node"):
        nodes[n.get("id")] = (float(n.get("lon")), float(n.get("lat")))
        t = {k.get("k"): k.get("v") for k in n.findall("tag")}
        if t:
            points.append((n.get("id"), t))
    tl = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    ids = list(nodes)
    ll = np.array([nodes[i] for i in ids])
    xs, ys = tl.transform(ll[:, 0], ll[:, 1])
    l93 = {i: (x, y) for i, x, y in zip(ids, xs, ys)}
    for w in root.findall("way"):
        refs = [nd.get("ref") for nd in w.findall("nd")]
        pts = [l93[r] for r in refs if r in l93]
        t = {k.get("k"): k.get("v") for k in w.findall("tag")}
        if len(pts) >= 2 and t:
            ways.append((t, pts, refs[0] == refs[-1]))
    pts = [(l93[i], t) for i, t in points if i in l93]
    return ways, pts


# -------------------------------------------------------------- mesh kit ----
# Kinds, read by the viewer's shader from the colour alpha.
GROUND, WALL_OLD, WALL_NEW, ROOF_FLAT, ROOF_TILE, LEAF, WOOD, HULL, WATER, PLAIN, WALL_GLASS = range(11)


class Mesh:
    def __init__(self):
        self.p, self.n, self.c, self.uv, self.i = [], [], [], [], []
        self.count = 0

    def add(self, pos, nrm, col, kind, uv=None, idx=None, style=None):
        pos = np.asarray(pos, np.float32)
        k = len(pos)
        nrm = np.broadcast_to(np.asarray(nrm, np.float32), (k, 3))
        col = np.broadcast_to(np.asarray(col, np.float32), (k, 3))
        rgba = np.column_stack([np.clip(col, 0, 255), np.full(k, kind * 16 + (8 if style is None else style))]).astype(np.uint8)
        uv = np.zeros((k, 2), np.float32) if uv is None else np.asarray(uv, np.float32)
        idx = np.arange(k) if idx is None else np.asarray(idx)
        self.p.append(pos); self.n.append(nrm); self.c.append(rgba); self.uv.append(uv)
        self.i.append(idx.astype(np.uint32).ravel() + self.count)
        self.count += k

    def tri(self, a, b, c, col, kind, uv=None, style=None):
        """Flat triangles; a, b, c: (k, 3) arrays."""
        a, b, c = (np.asarray(v, np.float32) for v in (a, b, c))
        nrm = np.cross(b - a, c - a)
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
        k = len(a)
        pos = np.stack([a, b, c], 1).reshape(-1, 3)
        nn = np.repeat(nrm, 3, 0)
        col = np.asarray(col, np.float32)
        col = np.repeat(col, 3, 0) if col.ndim == 2 else col
        self.add(pos, nn, col, kind, uv=None if uv is None else np.asarray(uv).reshape(-1, 2), style=style)

    def arrays(self):
        if not self.p:
            return None
        return (np.concatenate(self.p), np.concatenate(self.n), np.concatenate(self.c),
                np.concatenate(self.uv), np.concatenate(self.i))


def hexrgb(h):
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def rng_for(key):
    return np.random.default_rng(int(hashlib.md5(str(key).encode()).hexdigest()[:8], 16))


# ------------------------------------------------------------ sun shadows ----
SUN_TIME = (2026, 6, 21, 6, 15)     # UTC; viewer/filmset.js uses the same moment


def sun_position(lon, lat, when=SUN_TIME):
    """Solar azimuth (degrees from north, clockwise) and elevation (NOAA)."""
    import datetime
    t = datetime.datetime(*when, tzinfo=datetime.timezone.utc)
    jd = t.timestamp() / 86400 + 2440587.5
    jc = (jd - 2451545) / 36525
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    m_ = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    c = (math.sin(math.radians(m_)) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(math.radians(2 * m_)) * (0.019993 - 0.000101 * jc) + math.sin(math.radians(3 * m_)) * 0.000289)
    lam = l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * jc))
    eps = 23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    eps += 0.00256 * math.cos(math.radians(125.04 - 1934.136 * jc))
    dec = math.degrees(math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(lam))))
    y = math.tan(math.radians(eps / 2)) ** 2
    eot = 4 * math.degrees(y * math.sin(2 * math.radians(l0)) - 2 * e * math.sin(math.radians(m_))
                           + 4 * e * y * math.sin(math.radians(m_)) * math.cos(2 * math.radians(l0))
                           - 0.5 * y * y * math.sin(4 * math.radians(l0)) - 1.25 * e * e * math.sin(2 * math.radians(m_)))
    tst = (t.hour * 60 + t.minute + eot + 4 * lon) % 1440
    ha = tst / 4 - 180
    la, d_, h_ = math.radians(lat), math.radians(dec), math.radians(ha)
    zen = math.acos(math.sin(la) * math.sin(d_) + math.cos(la) * math.cos(d_) * math.cos(h_))
    az = math.degrees(math.atan2(math.sin(h_), math.cos(h_) * math.sin(la) - math.tan(d_) * math.cos(la))) + 180
    return az % 360, 90 - math.degrees(zen)


def sun_shadows(mns, sea):
    """Cast shadows of buildings, trees and hills on the ground at 1 m, from
    the LiDAR surface model and the same sun as the viewer. Painted into the
    ground map, so the streets have shadows even where the device cannot
    afford real-time ones."""
    az, el = sun_position(*ORIGIN)
    surf = np.where(np.isnan(mns), -5.0, mns)
    surf = surf.reshape(surf.shape[0] // 2, 2, surf.shape[1] // 2, 2).max(axis=(1, 3))
    n = surf.shape[0]
    rows, cols = np.mgrid[0:n, 0:n]
    shade = np.zeros(surf.shape, bool)
    tan_e = math.tan(math.radians(el))
    dr, dc = -math.cos(math.radians(az)), math.sin(math.radians(az))
    for k in range(1, int(min(250, 120 / max(tan_e, 0.05)))):
        r = np.clip(np.round(rows + dr * k).astype(int), 0, n - 1)
        c = np.clip(np.round(cols + dc * k).astype(int), 0, n - 1)
        shade |= surf[r, c] > surf + k * tan_e + 0.4
    print(f"sun az {az:.0f} el {el:.0f}, {shade.mean():.0%} in shade")
    return shade.astype(np.float32)


# ----------------------------------------------------------------- ground ----
def ground_mesh(mnt, sea):
    nx = int((X1 - X0) / GRID) + 1
    xs = X0 + np.arange(nx) * GRID
    ys = Y1 - np.arange(nx) * GRID
    X, Y = np.meshgrid(xs, ys)
    # MNT at 3 m: mean of the 50 cm cells around each node.
    h = ndimage.uniform_filter(np.nan_to_num(mnt, nan=0.0), size=5)
    w = ndimage.uniform_filter((~np.isnan(mnt)).astype(np.float32), size=5)
    valid = ndimage.uniform_filter((~sea).astype(np.float32), size=5)
    hm = np.where(w > 0, h / np.maximum(w, 1e-6), 0)
    r = np.clip(((Y1 - Y) / RES).astype(int), 0, mnt.shape[0] - 1)
    c = np.clip(((X - X0) / RES).astype(int), 0, mnt.shape[1] - 1)
    Z = hm[r, c]
    if TUNNEL is not None:
        # The deck over the tunnel, node by node (no smoothing into the mouths).
        Z = np.where(contains_xy(TUNNEL["ground"], X, Y), np.maximum(Z, TUNNEL["top"]), Z)
        # The road up to each mouth runs between retaining walls: keep the
        # 3 m ground grid from ramping up over it.
        from scipy.spatial import cKDTree
        near = contains_xy(TUNNEL["approach"], X, Y)
        if near.any():
            kd = cKDTree(np.column_stack([TUNNEL["x"], TUNNEL["y"]]))
            _, j = kd.query(np.column_stack([X[near], Y[near]]))
            Z[near] = np.minimum(Z[near], TUNNEL["z"][j])
    land = valid[r, c] > 0.5
    Z = np.where(land, Z, SEA_FLOOR)
    P = enu_fast(X.ravel(), Y.ravel(), Z.ravel())
    # Smooth normals from the height grid.
    gy, gx = np.gradient(Z, GRID)
    N = np.column_stack([-gx.ravel(), gy.ravel(), np.ones(Z.size)])  # y grid runs south
    N /= np.linalg.norm(N, axis=1, keepdims=True)
    uv = np.column_stack([(X.ravel() - X0) / (X1 - X0), (Y1 - Y.ravel()) / (Y1 - Y0)])
    ii = np.arange(nx * nx).reshape(nx, nx)
    a, b, cc, d = ii[:-1, :-1], ii[:-1, 1:], ii[1:, :-1], ii[1:, 1:]
    # Drop quads that are entirely sea floor (the sea plane covers them).
    seaq = (~land[:-1, :-1]) & (~land[:-1, 1:]) & (~land[1:, :-1]) & (~land[1:, 1:])
    keep = ~seaq
    if TUNNEL is not None:
        # The slope from the deck over the tunnel down to the road outside
        # would hang like a curtain across each mouth: leave it out.
        ins = contains_xy(TUNNEL["ground"], X, Y)
        endz = contains_xy(TUNNEL["ends"], X, Y) & ~ins
        anyq = lambda a_: a_[:-1, :-1] | a_[:-1, 1:] | a_[1:, :-1] | a_[1:, 1:]
        keep &= ~(anyq(ins) & anyq(endz))
    tris = np.concatenate([np.stack([a, cc, b], -1)[keep], np.stack([b, cc, d], -1)[keep]])
    m = Mesh()
    m.add(P, N, [255, 255, 255], GROUND, uv=uv, idx=tris)
    return m, Z


def paint_ground(ways, mnt, sea, bmask, canopy, ortho, shadow=None):
    S = 6144
    px = S / (X1 - X0)
    img = Image.new("RGB", (S, S), tuple(hexrgb("#d3c6ae")))
    d = ImageDraw.Draw(img)

    def xy(pts):
        return [((x - X0) * px, (Y1 - y) * px) for x, y in pts]

    def poly(pts, col):
        if len(pts) >= 3:
            d.polygon(xy(pts), fill=tuple(hexrgb(col)))

    def line(pts, col, width):
        p = xy(pts)
        w = max(1, int(width * px))
        d.line(p, fill=tuple(hexrgb(col)), width=w, joint="curve")
        r = w / 2
        for x, y in (p[0], p[-1]):
            d.ellipse([x - r, y - r, x + r, y + r], fill=tuple(hexrgb(col)))

    # Gardens and green areas.
    GREEN = {"grass": "#8fae5e", "park": "#86a85a", "garden": "#86a85a", "wood": "#6f9150",
             "forest": "#6f9150", "meadow": "#9ab468", "scrub": "#7f9a58", "orchard": "#8aa85c",
             "playground": "#c9b48e", "pitch": "#7fae66"}
    for t, pts, closed in ways:
        if not closed:
            continue
        k = t.get("leisure") or t.get("landuse") or t.get("natural")
        if k in GREEN:
            poly(pts, GREEN[k])
    # Ground under the LiDAR tree canopy: a darker garden green.
    can = Image.fromarray((canopy * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)
    can = can.filter(ImageFilter.GaussianBlur(3))
    img = Image.composite(Image.new("RGB", (S, S), tuple(hexrgb("#6d8a4c"))), img,
                          can.point(lambda v: int(min(255, v * 0.85))))
    d = ImageDraw.Draw(img)
    # Squares and pedestrian areas.
    for t, pts, closed in ways:
        if closed and (t.get("area") == "yes" or t.get("area:highway")) and t.get("highway") in (
                "pedestrian", "footway", "service") or t.get("place") == "square" and closed:
            poly(pts, "#e6dac3")
        if closed and t.get("amenity") == "parking" and t.get("parking") != "underground":
            poly(pts, "#9a9690")
    # Roads: pavement first, asphalt on top, markings last.
    WIDTH = {"motorway": 12, "trunk": 11, "primary": 10, "secondary": 8.5, "tertiary": 7.5,
             "residential": 6, "unclassified": 6, "service": 4.5, "living_street": 5,
             "primary_link": 6, "secondary_link": 6, "tertiary_link": 5, "raceway": 9}
    roads = [(t, pts) for t, pts, closed in ways
             if t.get("highway") in WIDTH and t.get("tunnel") not in ("yes", "building_passage")
             and t.get("area") != "yes" and t.get("layer", "0") not in ("-1", "-2", "-3")]
    for t, pts in roads:
        line(pts, "#e2d7c4", WIDTH[t["highway"]] + 4.5)
    for t, pts in roads:
        line(pts, "#cfc4b2", WIDTH[t["highway"]] + 1.2)       # kerb
    for t, pts in roads:
        line(pts, "#5d6068", WIDTH[t["highway"]])
    for t, pts, closed in ways:
        hw = t.get("highway")
        if hw in ("footway", "pedestrian", "path", "steps", "cycleway") and t.get("area") != "yes" \
                and t.get("tunnel") != "yes" and t.get("layer", "0") not in ("-1", "-2"):
            line(pts, "#cdbb9c" if hw == "steps" else "#e8dcc6", 3.0 if hw == "pedestrian" else 2.0)
    # Dashed centre lines on the larger roads.
    for t, pts in roads:
        if t["highway"] not in ("primary", "secondary", "tertiary", "trunk"):
            continue
        ls = LineString(pts)
        dist = np.arange(0, ls.length, 6.0)
        for s in dist:
            a, b = ls.interpolate(s), ls.interpolate(min(s + 3.0, ls.length))
            d.line(xy([(a.x, a.y), (b.x, b.y)]), fill=(236, 232, 220), width=max(1, int(0.25 * px)))
    # Pools and fountains.
    for t, pts, closed in ways:
        if closed and (t.get("leisure") == "swimming_pool" or t.get("amenity") == "fountain"
                       or (t.get("natural") == "water" and t.get("water") != "harbour")):
            poly(pts, "#45bfd0")
    # Soft shade at the foot of buildings (baked ambient occlusion).
    bm = Image.fromarray((bmask * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)
    ao = bm.filter(ImageFilter.GaussianBlur(int(4.5 * px)))
    arr = np.asarray(img).astype(np.float32)
    aof = np.asarray(ao).astype(np.float32) / 255
    arr *= (1 - 0.32 * aof)[..., None]
    # Sun shadows of buildings and trees, soft-edged and a little blue.
    if shadow is not None:
        sh = Image.fromarray((shadow * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)
        sh = np.asarray(sh.filter(ImageFilter.GaussianBlur(int(0.5 * px)))).astype(np.float32) / 255
        tint = np.array([0.60, 0.64, 0.74], np.float32)
        arr *= 1 - sh[..., None] * (1 - tint[None, None])
    # Shore: wet stone colour where the ground drops into the sea.
    seai = np.asarray(Image.fromarray((sea * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)) / 255
    arr = arr * (1 - seai[..., None]) + np.array(hexrgb("#2f6f86"))[None, None] * seai[..., None]
    # Paper-like grain so large areas do not look flat.
    g = np.random.default_rng(3).normal(0, 4, (S // 4, S // 4))
    g = np.asarray(Image.fromarray(g.astype(np.float32)).resize((S, S), Image.BILINEAR))
    arr += g[..., None]
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).resize((TEX, TEX), Image.LANCZOS)
    b = io.BytesIO()
    img.save(b, "JPEG", quality=86)
    img.save(RAW / "ground-preview.jpg", quality=80)
    return b.getvalue()


# -------------------------------------------------------------- buildings ----
# Belle Époque Riviera render: ochre, salmon, rose, butter yellow, cream.
WALL_OLD_COLS = ["#f0d39a", "#e8ad62", "#eb9d7a", "#f0aa98", "#f3d77f", "#f6ead2", "#dc7f58",
                 "#eec183", "#f4dcae", "#e5a27c", "#f2c6a6"]
WALL_NEW_COLS = ["#f4f2ec", "#e9e2d2", "#e2cfa8", "#efe8da", "#d9dde0", "#f2e3c4"]
ROOF_FLAT_COLS = ["#d2cabb", "#c9c0b0", "#bdb6aa", "#d8d0c2"]
ROOF_TILE_COLS = ["#c0603c", "#b6573a", "#c86c45", "#ad5236", "#c46a4a"]


def to_polys(g):
    if g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    return [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]


def cells_to_poly(mask, x0, y1, res):
    """Union of raster cells (mask True) as smoothed polygons."""
    rr, cc = np.nonzero(mask)
    if not len(rr):
        return Polygon()
    boxes = [box(x0 + c * res, y1 - (r + 1) * res, x0 + (c + 1) * res, y1 - r * res) for r, c in zip(rr, cc)]
    u = unary_union(boxes)
    return u.buffer(0.6, join_style=2).buffer(-1.2, join_style=2).buffer(0.6, join_style=2).simplify(0.8)


def triangulate(poly, area=None):
    poly = orient(poly, 1.0)
    rings = [poly.exterior] + list(poly.interiors)
    verts, segs, holes = [], [], []
    for k, ring in enumerate(rings):
        pts = np.asarray(ring.coords)[:-1, :2]
        if len(pts) < 3:
            continue
        base = len(verts)
        verts.extend(pts.tolist())
        n = len(pts)
        segs.extend([[base + i, base + (i + 1) % n] for i in range(n)])
        if k > 0:
            hp = Polygon(ring).representative_point()
            holes.append([hp.x, hp.y])
    data = {"vertices": np.array(verts), "segments": np.array(segs)}
    if holes:
        data["holes"] = np.array(holes)
    opts = "pq25" + (f"a{area}" if area else "")
    try:
        t = triangle.triangulate(data, opts)
    except Exception:
        return None
    if "triangles" not in t:
        return None
    return t["vertices"], t["triangles"]


def add_walls(m, poly, z0, z1, base, fh, col, kind, inward=False, style=None):
    poly = orient(poly, -1.0 if inward else 1.0)
    for ring in [poly.exterior] + list(poly.interiors):
        pts = np.asarray(ring.coords)[:, :2]
        a, b = pts[:-1], pts[1:]
        L = np.linalg.norm(b - a, axis=1)
        ok = L > 0.3
        a, b, L = a[ok], b[ok], L[ok]
        if not len(a):
            continue
        k = len(a)
        bay = STYLE_BAY[style] if style is not None else {WALL_OLD: 3.2, WALL_NEW: 3.4, WALL_GLASS: 2.2}.get(kind, 3.0)
        nb = np.maximum(1, np.round(L / bay))
        P = enu_fast(np.column_stack([a[:, 0], b[:, 0], b[:, 0], a[:, 0]]).ravel(),
                     np.column_stack([a[:, 1], b[:, 1], b[:, 1], a[:, 1]]).ravel(),
                     np.tile([z0, z0, z1, z1], k)).reshape(k, 4, 3)
        nrm = np.cross(P[:, 1] - P[:, 0], P[:, 3] - P[:, 0])
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
        v0, v1 = (z0 - base) / fh, (z1 - base) / fh
        uv = np.stack([np.column_stack([np.zeros(k), nb, nb, np.zeros(k)]),
                       np.column_stack([np.full(k, v0), np.full(k, v0), np.full(k, v1), np.full(k, v1)])], -1)
        idx = (np.arange(k)[:, None] * 4 + np.array([0, 1, 2, 0, 2, 3])[None]).ravel()
        m.add(P.reshape(-1, 3), np.repeat(nrm, 4, 0), col, kind, uv=uv.reshape(-1, 2), idx=idx, style=style)


def add_roof(m, poly, z, col, tiled, cap=None):
    if tiled:
        t = triangulate(poly, area=4)
    else:
        t = triangulate(poly)
    if t is None:
        return
    v, tri = t
    if tiled:
        import shapely
        dist = shapely.distance(poly.exterior if not poly.interiors else poly.boundary,
                                shapely.points(v[:, 0], v[:, 1]))
        zz = z + np.minimum(dist * math.tan(math.radians(28)), cap if cap else 99)
    else:
        zz = np.full(len(v), z)
    P = enu_fast(v[:, 0], v[:, 1], zz)
    m.tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], col, ROOF_TILE if tiled else ROOF_FLAT)


def kmeans1d(v, k):
    c = np.percentile(v, np.linspace(10, 90, k))
    for _ in range(20):
        lab = np.argmin(np.abs(v[:, None] - c[None]), 1)
        c = np.array([v[lab == i].mean() if np.any(lab == i) else c[i] for i in range(k)])
    lab = np.argmin(np.abs(v[:, None] - c[None]), 1)
    return np.sort(c), lab


# Hand-picked looks for landmark buildings (IGN BD TOPO ids).
HEROES = {
    # Casino de Monte-Carlo and the Salle Garnier: cream stone, verdigris roofs.
    "BATIMENT0000000074070294": {"wall": "#f3e4c4", "roof": "#6fa596"},
    "BATIMENT0000000074070299": {"wall": "#f3e4c4", "roof": "#6fa596"},
}


# Façade styles, read by the viewer's shader from the low bits of the colour
# alpha (kind * 16 + style). Each one is a real type of Monaco building.
(S_SHUTTER, S_BELLE, S_ARCH, S_VILLA, S_LOGGIA, S_RIBBON, S_GLASS, S_MODERN, S_DECO) = range(9)
STYLE_KIND = {S_SHUTTER: WALL_OLD, S_BELLE: WALL_OLD, S_ARCH: WALL_OLD, S_VILLA: WALL_OLD, S_DECO: WALL_OLD,
              S_LOGGIA: WALL_NEW, S_RIBBON: WALL_NEW, S_MODERN: WALL_NEW, S_GLASS: WALL_GLASS}
# Bay width and storey height, m.
STYLE_BAY = {S_SHUTTER: 3.1, S_BELLE: 3.5, S_ARCH: 4.6, S_VILLA: 3.0, S_LOGGIA: 3.4, S_RIBBON: 3.6,
             S_GLASS: 2.2, S_MODERN: 3.0, S_DECO: 3.4}
STYLE_FH = {S_SHUTTER: 3.3, S_BELLE: 3.8, S_ARCH: 5.2, S_VILLA: 3.1, S_LOGGIA: 3.0, S_RIBBON: 2.95,
            S_GLASS: 3.4, S_MODERN: 3.3, S_DECO: 3.2}

# Landmarks around the square, by a point (Lambert-93) inside or next to
# their outline: style and the colour of the real façade.
LANDMARKS = [
    ("Hôtel de Paris", (1056550, 6303330), S_BELLE, "#f5eedf"),
    ("Hôtel de Paris", (1056523, 6303375), S_BELLE, "#f5eedf"),
    ("Hôtel Hermitage", (1056432, 6303262), S_BELLE, "#f2d9bd"),
    ("Café de Paris", (1056577, 6303396), S_ARCH, "#f6f0e4"),
    ("Hôtel Métropole", (1056574, 6303546), S_BELLE, "#f1dcc0"),
    ("One Monte-Carlo", (1056448, 6303367), S_MODERN, "#eeede8"),
    ("One Monte-Carlo", (1056433, 6303407), S_MODERN, "#eeede8"),
    ("Fairmont", (1056743, 6303450), S_RIBBON, "#eee8dc"),
    ("Théâtre Princesse Grace", (1056443, 6303173), S_DECO, "#efe2c8"),
    ("Yacht Club de Monaco", (1056540, 6303156), S_MODERN, "#f2f2ee"),
    ("Église Saint-Charles", (1056513, 6303731), S_ARCH, "#e9dcc4"),
]


def style_for_year(y, H):
    if y < 1925:
        return S_BELLE if H > 14 else S_VILLA
    if y < 1950:
        return S_DECO
    if y < 1995:
        return S_RIBBON if H > 30 else S_LOGGIA
    return S_GLASS if H > 60 else S_MODERN


class FacadeData:
    """What open data says about each building: floors (OpenStreetMap,
    Wikidata, BD TOPO), year built (OpenStreetMap, Wikidata), name."""

    def __init__(self, ways):
        from shapely.strtree import STRtree
        self.osm = []
        for t, pts, closed in ways:
            if closed and len(pts) > 3 and ("building" in t or "building:part" in t):
                try:
                    g = Polygon(pts).buffer(0)
                except Exception:
                    continue
                if g.area > 8:
                    self.osm.append((g, t))
        self.tree = STRtree([g for g, _ in self.osm])
        self.wd = []
        f = RAW / "wikidata.json"
        tl = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
        if f.exists():
            for b in json.load(open(f))["results"]["bindings"]:
                g = lambda k: b.get(k, {}).get("value", "")
                lon, lat = map(float, g("coord")[6:-1].split())
                x, y = tl.transform(lon, lat)
                self.wd.append((shapely.Point(x, y), g("itemLabel"), g("inc")[:4], g("styleLabel"), g("floors")))
        self.marks = [(shapely.Point(*xy), name, st, colour) for name, xy, st, colour in LANDMARKS]

    def get(self, poly, props):
        out = {"levels": None, "year": None, "name": None, "style": None, "colour": None}
        for i in self.tree.query(poly):
            g, t = self.osm[i]
            inter = g.intersection(poly).area
            if inter < 0.5 * min(g.area, poly.area):
                continue
            lv = t.get("building:levels", "")
            if lv.replace(".", "").isdigit() and not out["levels"]:
                out["levels"] = float(lv)
            sd = t.get("start_date", "")[:4]
            if sd.isdigit() and not out["year"]:
                out["year"] = int(sd)
            out["name"] = out["name"] or t.get("name")
        big = poly.buffer(4)
        for pt, label, inc, st, fl in self.wd:
            if not big.contains(pt):
                continue
            if inc.isdigit() and not out["year"] and 1850 < int(inc) < 2030 and "Festival" not in label:
                out["year"] = int(inc)
            if fl.isdigit() and not out["levels"]:
                out["levels"] = float(fl)
            if st in ("Belle Époque", "Beaux-Arts"):
                out["style"] = S_BELLE
        if not out["levels"] and props.get("nombre_d_etages"):
            out["levels"] = float(props["nombre_d_etages"]) + 1   # BD TOPO counts floors above the ground floor
        if not out["year"] and (props.get("date_d_apparition") or "")[:4].isdigit():
            out["year"] = int(props["date_d_apparition"][:4])
        for pt, name, st, colour in self.marks:
            if poly.buffer(3).contains(pt):
                out.update(style=st, colour=colour, name=name)
        return out


def pick_style(info, H, tiled, grand, post_scan, rnd):
    """Real data first, then what the building looks like from above."""
    if info["style"] is not None:
        return info["style"]
    if post_scan:
        return S_MODERN
    if info["year"]:
        return style_for_year(info["year"], H)
    name = (info["name"] or "").lower()
    lv = info["levels"] or H / 3.0
    if name.startswith(("villa", "maison", "castel", "casa")) and lv <= 7:
        return S_VILLA
    if any(w in name for w in ("palais", "palace", "château", "chateau")) and lv <= 10:
        return S_BELLE
    if grand:
        return S_BELLE
    if tiled:
        return S_VILLA if H < 13 else S_SHUTTER
    r = rnd.random()
    if H > 40:          # the 1960s-80s towers: balconies all round
        return S_RIBBON if r < 0.8 else S_LOGGIA
    if H > 20:
        return S_LOGGIA if r < 0.5 else (S_RIBBON if r < 0.75 else S_SHUTTER)
    return S_SHUTTER if r < 0.45 else (S_VILLA if r < 0.62 else (S_DECO if r < 0.77 else S_LOGGIA))


def roof_colour(rgb):
    """Stylised roof colour from the median IGN photo colour of the roof:
    a little brighter and more saturated, like a painted set."""
    c = np.asarray(rgb, float)
    g = c.mean()
    c = g + (c - g) * 1.35
    c = c * 1.15 + 22
    return np.clip(c, 60, 238)


def roof_kind(rgb):
    r_, g_, b_ = rgb
    if r_ > 95 and r_ > g_ * 1.22 and r_ > b_ * 1.35:
        return "tile"
    if g_ > r_ * 1.12 and b_ > r_ * 1.02 and g_ > 80 and (max(rgb) - min(rgb)) > 18:
        return "copper"          # verdigris roofs: Casino, Hôtel de Paris…
    return "flat"


def add_mansard(m, poly, z, col, kind, slope_h=3.2, inset=2.4):
    """Steep roof skirt rising slope_h over the first `inset` metres, then flat."""
    t = triangulate(poly, area=6)
    if t is None:
        return
    v, tri = t
    dist = shapely.distance(poly.boundary, shapely.points(v[:, 0], v[:, 1]))
    zz = z + np.minimum(dist / inset, 1.0) * slope_h
    P = enu_fast(v[:, 0], v[:, 1], zz)
    m.tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], col, kind)


def add_cap(m, poly, z, h, col, kind):
    """Pyramid / dome-like cap on a small tower outline."""
    c = poly.centroid
    pts = np.asarray(orient(poly, 1.0).exterior.coords)[:-1]
    k = len(pts)
    ring = enu_fast(pts[:, 0], pts[:, 1], np.full(k, z))
    mid = enu_fast(pts[:, 0] * 0.45 + c.x * 0.55, pts[:, 1] * 0.45 + c.y * 0.55, np.full(k, z + h * 0.7))
    top = enu_fast(np.array([c.x]), np.array([c.y]), np.array([z + h]))[0]
    j = np.arange(k); jn = (j + 1) % k
    m.tri(ring[j], ring[jn], mid[jn], col, kind)
    m.tri(ring[j], mid[jn], mid[j], col, kind)
    m.tri(mid[j], mid[jn], np.repeat(top[None], k, 0), col, kind)


def buildings(m, feats, mns, mnt, ortho, stats, facades):
    mntR = Raster(mnt)
    extent = box(X0, Y0, X1, Y1)
    footprint = []
    for f in feats:
        g = shapely.force_2d(shape(f["geometry"])).buffer(0)
        for poly in to_polys(g):
            if poly.area < 10 or not extent.contains(poly.centroid):
                continue
            poly = orient(poly.simplify(0.35), 1.0)
            if not isinstance(poly, Polygon) or poly.is_empty:
                continue
            key = f["properties"].get("cleabs") or poly.wkt[:60]
            rnd = rng_for(key)
            # Ground: lowest terrain along the outline.
            ring = poly.exterior
            s = np.linspace(0, ring.length, max(8, int(ring.length / 1.0)))
            pts_ = shapely.line_interpolate_point(ring, s)
            bx, by = shapely.get_x(pts_), shapely.get_y(pts_)
            gz = mntR.sample(bx, by)
            if np.all(np.isnan(gz)):
                continue
            base = float(np.nanmin(gz))
            # Roof heights: MNS cells inside the outline, 1 m apart.
            x0, y0, x1, y1 = poly.bounds
            c0, c1 = max(int((x0 - X0) / RES), 0), int(math.ceil((x1 - X0) / RES))
            r0, r1 = max(int((Y1 - y1) / RES), 0), int(math.ceil((Y1 - y0) / RES))
            sub = mns[r0:r1:2, c0:c1:2]
            if sub.size == 0:
                continue
            gx, gy = cell_xy(r0 + 2 * np.arange(sub.shape[0])[:, None] + 0.5,
                             c0 + 2 * np.arange(sub.shape[1])[None, :] + 0.5)
            gx, gy = np.broadcast_arrays(gx, gy)
            inner = poly.buffer(-0.6)
            ins = contains_xy(inner if not inner.is_empty else poly, gx, gy) & np.isfinite(sub)
            if ins.sum() < 4:
                continue
            hrel = sub[ins] - base
            top = float(np.percentile(hrel, 92))
            props = f["properties"]
            # The LiDAR was flown in 2021; BD TOPO is current. A building
            # the scan does not see is either newer (Mareterra and its
            # neighbours, 2022 onwards: keep it, at its BD TOPO height, in a
            # modern style) or something that no longer stands, a shed or a
            # canopy (drop it: these were the stray blocks).
            built = float(np.mean(hrel > 2.5))
            recent = (props.get("date_creation") or "") >= "2022"
            post_scan = False
            if built < 0.35:
                if not (recent and props.get("hauteur")):
                    stats["dropped"] += 1
                    continue
                post_scan = True
                top = float(props["hauteur"])
                if props.get("altitude_minimale_sol") is not None:
                    base = float(props["altitude_minimale_sol"])
            if top < 2.5:
                continue
            cx = ((gx[ins] - X0) / RES).astype(int); cy = ((Y1 - gy[ins]) / RES).astype(int)
            rgb = np.median(ortho[np.clip(cy, 0, ortho.shape[0] - 1), np.clip(cx, 0, ortho.shape[1] - 1)].astype(float), 0)
            rk = roof_kind(rgb)
            tiled = rk == "tile"
            rcol = roof_colour(rgb)
            # Steps: podium / body / tower from the spread of roof heights.
            levels = [(poly, top)]
            if post_scan:
                rk, tiled, rcol = "flat", False, np.array(hexrgb("#d9d6cf"))
            if not post_scan and poly.area > 150 and np.percentile(hrel, 95) - np.percentile(hrel, 15) > 6 and not tiled:
                k = 3 if poly.area > 1500 else 2
                cen, lab = kmeans1d(hrel, k)
                keep = [0]
                for i in range(1, k):
                    if cen[i] - cen[keep[-1]] > 4.5:
                        keep.append(i)
                if len(keep) > 1:
                    lab2 = np.searchsorted(cen[keep][:-1] + np.diff(cen[keep]) / 2, hrel)
                    grid = np.full(sub.shape, -1)
                    grid[ins] = lab2
                    levels, prev = [], poly
                    for li in range(len(keep)):
                        sel = hrel[lab2 >= li]
                        hz = float(np.percentile(sel, 80))
                        if li == 0:
                            region = poly
                        else:
                            m_ = ndimage.binary_opening(grid >= li, iterations=2)
                            region = cells_to_poly(m_, X0 + c0 * RES, Y1 - r0 * RES, 2 * RES)
                            region = region.intersection(prev).simplify(1.2)
                        parts = [p for p in to_polys(region) if p.area > 60
                                 and p.area / max(p.convex_hull.area, 1) > 0.55]
                        if not parts:
                            break
                        region = unary_union(parts)
                        levels.append((region, max(hz, 3.0)))
                        prev = region
                    if not levels:
                        levels = [(poly, top)]
                    for i in range(1, len(levels)):
                        if levels[i][1] < levels[i - 1][1] + 3:
                            levels = levels[:i]
                            break
            H = levels[-1][1]
            hero = HEROES.get(props.get("cleabs"))
            if hero:
                rk, tiled = "copper", False
                rcol = np.array(hexrgb(hero["roof"]))
            grand = not post_scan and (rk == "copper" or (not tiled and H < 30 and poly.area > 500
                                                          and rnd.random() < 0.35))
            info = facades.get(poly, props)
            style = S_ARCH if hero else pick_style(info, H, tiled, grand, post_scan, rnd)
            if info["style"] == S_BELLE and not tiled and not post_scan and poly.area > 200:
                grand = True        # a real palace: stone, mansard roof
            if style not in (S_BELLE, S_ARCH):
                grand = False
            kind = STYLE_KIND[style]
            stats["styles"][style] += 1
            pal = {S_BELLE: ["#f2e3c2", "#efd9b0", "#f4e8cf", "#f3e6d0", "#f0d8bc"],
                   S_ARCH: ["#f3e4c4"],
                   S_VILLA: ["#e8ad62", "#eb9d7a", "#f0aa98", "#f3d77f", "#dc7f58", "#f0c27b", "#f2cf9a"],
                   S_DECO: ["#f1e6cf", "#ead9b8", "#f4efe4", "#e6d2b5", "#f0dcc4"],
                   S_RIBBON: ["#efe9dd", "#e6dccb", "#f3f0ea", "#dcd2bf", "#e9e0d0"],
                   }.get(style, WALL_OLD_COLS if kind == WALL_OLD else WALL_NEW_COLS)
            col = np.array(hexrgb(rnd.choice(pal))) * (0.95 + 0.07 * rnd.random())
            if info["colour"]:
                col = np.array(hexrgb(info["colour"]))
            if hero:
                col = np.array(hexrgb(hero["wall"]))
            # Storeys: the real count where open data has it, so each
            # building gets its own number of rows of windows.
            hwall = max(2.5, float(np.percentile(hrel, 40))) if tiled else H
            fh = STYLE_FH[style]
            if info["levels"] and 2.5 <= hwall / info["levels"] <= 5.8:
                fh = hwall / info["levels"]
                stats["real_floors"] += 1
            elif len(levels) == 1:
                fh = hwall / max(1, round(hwall / fh))
            zb = base - 1.0
            for li, (region, hz) in enumerate(levels):
                zt = base + hz
                last = li == len(levels) - 1
                for part in tunnel_split(m, region, zb, zt, base, fh, col, kind, style, rcol):
                    if kind == WALL_OLD and part.area > 60:
                        # Stone cornice under the roof line: a moulding
                        # that juts out and catches the light.
                        ztop = (base + max(2.5, float(np.percentile(hrel, 40)))) if tiled else \
                            (zt - min(3.4, hz * 0.25) if grand and last and part.area > 120 and hz > 8 else zt)
                        cor = part.buffer(0.4, join_style=2)
                        if isinstance(cor, Polygon):
                            cc_ = np.minimum(np.asarray(col) * 1.08 + 8, 250)
                            add_walls(m, cor, ztop - 0.6, ztop, ztop, 10, cc_, PLAIN)
                            for rp in to_polys(cor.difference(part)):
                                add_roof(m, orient(rp, 1.0), ztop, cc_, False)
                    if tiled:
                        eave = base + max(2.5, float(np.percentile(hrel, 40)))
                        add_walls(m, part, zb, eave, base, fh, col, kind, style=style)
                        add_roof(m, part, eave, rcol, True, max(1.0, base + top - eave))
                    elif grand and last and part.area > 120 and hz > 8:
                        sk = min(3.4, hz * 0.25)
                        add_walls(m, part, zb, zt - sk, base, fh, col, kind, style=style)
                        add_mansard(m, part, zt - sk, rcol if rk == "copper" else hexrgb("#7d8a8f"),
                                    ROOF_TILE, sk, 2.2)
                    else:
                        add_walls(m, part, zb, zt, base, fh, col, kind, style=style)
                        add_roof(m, part, zt, rcol, False)
                        if part.area > 40:
                            # Parapet: a low wall round the roof edge, so
                            # roofs read as terraces instead of lids.
                            pc = np.asarray(col) * 0.97
                            add_walls(m, part, zt, zt + 0.9, zt, 10, pc, PLAIN)
                            add_walls(m, part, zt, zt + 0.9, zt, 10, pc * 0.85, PLAIN, inward=True)
                            inner_top = part.buffer(-0.25, join_style=2)
                            ring_ = part.difference(inner_top) if not inner_top.is_empty else part
                            for rp in to_polys(ring_):
                                add_roof(m, rp, zt + 0.9, pc * 1.05, False)
                zb = zt
            # Roof gardens: where the IGN photo shows green on a flat roof,
            # plant shrubs (Monaco has many planted terraces).
            if not tiled and not grand and not post_scan:
                cell_rgb = ortho[np.clip(cy, 0, ortho.shape[0] - 1), np.clip(cx, 0, ortho.shape[1] - 1)].astype(float)
                exg = 2 * cell_rgb[:, 1] - cell_rgb[:, 0] - cell_rgb[:, 2]
                green = np.nonzero((exg > 18) & (np.abs(hrel - H) < 2.5))[0]
                if len(green) >= 6:
                    gxy = np.column_stack([gx[ins][green], gy[ins][green]])
                    picked = []
                    for k_ in rnd.permutation(len(gxy)):
                        if len(picked) >= 14:
                            break
                        if all(np.hypot(*(gxy[k_] - q_)) > 2.6 for q_ in picked):
                            picked.append(gxy[k_])
                    for q_ in picked:
                        add_tree(m, q_[0], q_[1], base + H, rnd.uniform(1.4, 2.6), rnd.uniform(0.8, 1.4),
                                 "broad", rnd)
                        stats["roof_plants"] += 1
            # Rooftop machinery rooms and stair towers seen by the LiDAR.
            if not tiled and not grand and not post_scan and len(levels) == 1 and poly.area > 150:
                grid = np.zeros(sub.shape, bool)
                grid[ins] = (hrel > H + 1.6) & (hrel < H + 6)
                lab_, n_ = ndimage.label(ndimage.binary_opening(grid, iterations=1))
                for j in range(1, min(n_, 8) + 1):
                    cells = lab_ == j
                    a = cells.sum() * (2 * RES) ** 2
                    if a < 6 or a > 150:
                        continue
                    hv = sub[cells] - base
                    bp = cells_to_poly(cells, X0 + c0 * RES, Y1 - r0 * RES, 2 * RES)
                    bp = bp.minimum_rotated_rectangle if not bp.is_empty else bp
                    bp = bp.intersection(poly.buffer(-1.0))
                    for q in to_polys(bp):
                        if q.area < 4:
                            continue
                        q = orient(q, 1.0)
                        zt2 = base + min(float(np.percentile(hv, 80)), H + 4.5)
                        mc = np.array(hexrgb("#dcd8cf")) * (0.92 + 0.08 * rnd.random())
                        add_walls(m, q, base + H, zt2, base + H, 10, mc, PLAIN)
                        add_roof(m, q, zt2, mc * 0.9, False)
                        stats["rooftop"] += 1
            # Towers and domes: small blobs that rise well above the roof.
            if grand and poly.area > 300:
                ref = np.percentile(hrel, 70)
                grid = np.zeros(sub.shape, bool)
                grid[ins] = hrel > ref + 5
                lab_, n_ = ndimage.label(ndimage.binary_opening(grid, iterations=1))
                for j in range(1, min(n_, 6) + 1):
                    cells = lab_ == j
                    a = cells.sum() * (2 * RES) ** 2
                    if a < 12 or a > 400:
                        continue
                    hv = sub[cells] - base
                    tp = cells_to_poly(cells, X0 + c0 * RES, Y1 - r0 * RES, 2 * RES).convex_hull
                    if tp.is_empty or not isinstance(tp, Polygon):
                        continue
                    tp = orient(tp.simplify(0.8), 1.0)
                    body = float(np.percentile(hv, 50))
                    peak = float(np.max(hv))
                    # Thin, very tall blobs are cranes or masts, not towers.
                    if peak - ref > 20 or peak - body > 2 * math.sqrt(a) + 3 \
                            or tp.area / max(tp.minimum_rotated_rectangle.length ** 2 / 16, 1) < 0.35:
                        continue
                    add_walls(m, tp, base + ref, base + body, base, fh, col, kind, style=style)
                    add_cap(m, tp, base + body, max(1.5, peak - body),
                            rcol if rk == "copper" else hexrgb("#6f8b84"), ROOF_TILE)
                    stats["towers"] += 1
            footprint.append(poly)
            stats["buildings"] += 1
            stats["levels"] += len(levels)
    return footprint


# ----------------------------------------------------------------- tunnel ----
# The Grand Prix tunnel runs under the Fairmont hotel. The LiDAR ground model
# fills it in (the scan only sees its roof), so the set carves it out: the
# ground over it is lifted to a deck above the vault, buildings start above
# it, and an open tube with a road, walls and a ceiling is built inside.
# The centre line and road height are the lap tours' (viewer/lap-route.js),
# so the tours' own lit tunnel (viewer/clean-track.js) fits inside.
TUNNEL = None
TUBE_HALF, TUBE_H = 6.7, 6.5


def load_tunnel():
    f = ROOT / "viewer" / "lap-route.js"
    if not f.exists():
        return None
    txt = f.read_text()
    d = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    if not d.get("tunnels"):
        return None
    P = np.array(d["points"])
    tl = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    x, y = tl.transform(P[:, 0], P[:, 1])
    # Smoothed like the tours' track (clean-track.js: 2 and 3 samples).
    x = ndimage.gaussian_filter1d(np.asarray(x), 2, mode="wrap")
    y = ndimage.gaussian_filter1d(np.asarray(y), 2, mode="wrap")
    z = ndimage.gaussian_filter1d(P[:, 2] - GEOID_N, 3, mode="wrap")
    s = np.arange(len(P)) * d["step"]
    ta, tb = d["tunnels"][0]

    def line(a, b):
        sel = (s >= a) & (s <= b)
        return LineString(np.column_stack([x[sel], y[sel]]))
    inside = (s >= ta - 1) & (s <= tb + 1)
    t = {"ta": ta, "tb": tb, "x": x, "y": y, "z": z, "s": s,
         "top": float(z[inside].max()) + TUBE_H + 0.4,
         "ground": line(ta - 1, tb + 1).buffer(8.5, cap_style=2),
         # Buildings over the road near the mouths (the hotel overhangs it)
         # start above the vault too.
         "bld": line(ta - 14, tb + 14).buffer(7.2, cap_style=2),
         "approach": unary_union([line(ta - 16, ta + 1).buffer(6.2, cap_style=2),
                                  line(tb - 1, tb + 16).buffer(6.2, cap_style=2)]),
         "ends": unary_union([line(ta - 9, ta + 2).buffer(9.5, cap_style=2),
                              line(tb - 2, tb + 9).buffer(9.5, cap_style=2)])}
    return t


def tunnel_frame(s_):
    t = TUNNEL
    x = np.interp(s_, t["s"], t["x"]); y = np.interp(s_, t["s"], t["y"]); z = np.interp(s_, t["s"], t["z"])
    dx = np.interp(s_ + 1.5, t["s"], t["x"]) - np.interp(s_ - 1.5, t["s"], t["x"])
    dy = np.interp(s_ + 1.5, t["s"], t["y"]) - np.interp(s_ - 1.5, t["s"], t["y"])
    l = np.hypot(dx, dy)
    return x, y, z, dx / l, dy / l          # left = (-dy, dx)


def tunnel_split(m, region, zb, zt, base, fh, col, kind, style, rcol):
    """Building parts to build as usual; the part over the tunnel starts
    above the vault."""
    parts = to_polys(region)
    if TUNNEL is None:
        return parts
    out = []
    for part in parts:
        if not part.intersects(TUNNEL["bld"]):
            out.append(part)
            continue
        top = TUNNEL["top"]
        if zt > top + 1:
            for q in to_polys(part.intersection(TUNNEL["bld"])):
                if q.area > 2:
                    q = orient(q, 1.0)
                    add_walls(m, q, max(zb, top), zt, base, fh, col, kind, style=style)
                    add_roof(m, q, zt, rcol, False)
        out += [orient(q, 1.0) for q in to_polys(part.difference(TUNNEL["bld"]).buffer(0)) if q.area > 2]
    return out


TUBE_LIT, TUBE_LAMP = 1, 2          # PLAIN styles: self-lit tunnel surfaces, lamps


def tri_facing(m, a, b, c, toward, col, kind, style=None):
    """Triangles whose front face looks toward the given points."""
    n = np.cross(b - a, c - a)
    flip = np.einsum("ij,ij->i", n, toward - a) < 0
    b2, c2 = np.where(flip[:, None], c, b), np.where(flip[:, None], b, c)
    m.tri(a, b2, c2, col, kind, style=style)


def tunnel_tube(m):
    t = TUNNEL
    ta, tb = t["ta"], t["tb"]
    W, Hh = TUBE_HALF, TUBE_H
    prof = [(-W, -0.3), (-W, 4.6), (-W + 1.3, 5.7), (-2.8, 6.3), (0, Hh), (2.8, 6.3), (W - 1.3, 5.7), (W, 4.6), (W, -0.3)]
    ss = np.arange(ta - 1, tb + 1.01, 1.5)
    x, y, z, fx, fy = tunnel_frame(ss)
    lx, ly = -fy, fx
    R = np.array([[enu_fast(x + lx * w, y + ly * w, z + h) for w, h in prof]])[0]   # (len(prof), n, 3)
    axis = enu_fast(x, y, z + 2.8)
    wall, ceil = np.array(hexrgb("#d8d4c8")), np.array(hexrgb("#55534f"))
    for j in range(len(prof) - 1):
        a, b, c, d = R[j, :-1], R[j + 1, :-1], R[j, 1:], R[j + 1, 1:]
        ax = axis[:-1]
        col = ceil if prof[j][1] > 5 and prof[j + 1][1] > 5 else wall
        tri_facing(m, a, b, c, ax, col, PLAIN, TUBE_LIT)
        tri_facing(m, b, d, c, ax, col, PLAIN, TUBE_LIT)
    # Two rows of lamps along the ceiling.
    for w in (-2.4, 2.4):
        for k in range(0, len(ss) - 1, 2):
            s0 = np.array([ss[k], ss[k] + 1.1])
            x_, y_, z_, fx_, fy_ = tunnel_frame(s0)
            h = 6.3 - 0.06 + (Hh - 6.3) * (1 - abs(w) / 2.8)
            p0 = enu_fast(x_ - fy_ * (w - 0.25), y_ + fx_ * (w - 0.25), z_ + h)
            p1 = enu_fast(x_ - fy_ * (w + 0.25), y_ + fx_ * (w + 0.25), z_ + h)
            dn = (p0 + p1) / 2 - np.array([0, 0, 3.0])
            tri_facing(m, p0[:1], p1[:1], p0[1:], dn[:1], hexrgb("#fff1c8"), PLAIN, TUBE_LAMP)
            tri_facing(m, p1[:1], p1[1:], p0[1:], dn[:1], hexrgb("#fff1c8"), PLAIN, TUBE_LAMP)
    # Road and kerbs inside.
    for w0, w1, colr, dz in [(-W, W, "#3d3e40", 0.02), (-W, -W + 0.9, "#b9b5ab", 0.2), (W - 0.9, W, "#b9b5ab", 0.2)]:
        a, b = enu_fast(x + lx * w0, y + ly * w0, z + dz), enu_fast(x + lx * w1, y + ly * w1, z + dz)
        up = (a + b) / 2 + np.array([0, 0, 5.0])
        tri_facing(m, a[:-1], b[:-1], a[1:], up[:-1], hexrgb(colr), PLAIN, TUBE_LIT)
        tri_facing(m, b[:-1], b[1:], a[1:], up[:-1], hexrgb(colr), PLAIN, TUBE_LIT)
    # Mouths: a stone face from the vault up to the deck, and to the sides.
    stone = np.array(hexrgb("#cdc3ae"))
    for s0, out_ in ((ta - 1, -1), (tb + 1, 1)):
        x0, y0, z0, fx0, fy0 = (float(v[0]) for v in tunnel_frame(np.array([s0])))
        lx0, ly0 = -fy0, fx0
        q = lambda w, h: enu_fast(np.array([x0 + lx0 * w]), np.array([y0 + ly0 * w]), np.array([z0 + h]))[0]
        front = enu_fast(np.array([x0 + fx0 * out_ * 10]), np.array([y0 + fy0 * out_ * 10]), np.array([z0 + 3]))[0]
        top = t["top"] - z0 + 0.3
        for w0, w1, h0, h1 in ((-9.5, 9.5, Hh - 0.2, top), (-9.5, -W, -0.5, Hh), (W, 9.5, -0.5, Hh)):
            A, B, C, D = q(w0, h0), q(w1, h0), q(w0, h1), q(w1, h1)
            tri_facing(m, np.array([A, B]), np.array([B, D]), np.array([C, C]), np.array([front, front]), stone, PLAIN)


# ------------------------------------------------------------------ trees ----
def unit_sphere(nu=7, nv=4):
    verts = []
    for j in range(nv + 1):
        th = math.pi * j / nv
        for i in range(nu):
            ph = 2 * math.pi * i / nu + (j % 2) * math.pi / nu
            verts.append([math.sin(th) * math.cos(ph), math.sin(th) * math.sin(ph), math.cos(th)])
    verts = np.array(verts)
    tris = []
    for j in range(nv):
        for i in range(nu):
            a = j * nu + i; b = j * nu + (i + 1) % nu
            c = (j + 1) * nu + i; d = (j + 1) * nu + (i + 1) % nu
            tris += [[a, c, b], [b, c, d]]
    tris = np.array(tris)
    return verts, tris


SPH_V, SPH_T = unit_sphere()


def cone(nu=6):
    v = [[math.cos(2 * math.pi * i / nu), math.sin(2 * math.pi * i / nu), 0] for i in range(nu)] + [[0, 0, 1]]
    t = [[i, (i + 1) % nu, nu] for i in range(nu)]
    return np.array(v), np.array(t)


CONE_V, CONE_T = cone()


def add_tree(m, x, y, z, h, r, kind, rnd):
    """x, y Lambert-93, z ground (NGF)."""
    def put(v, t, scale, offset, col, k, jitter=0.0):
        vv = v * scale + offset
        if jitter:
            vv = vv + rnd.normal(0, jitter, vv.shape) * scale
        P = enu_fast(x + vv[:, 0], y + vv[:, 1], z + vv[:, 2])
        if k != LEAF:
            m.tri(P[t[:, 0]], P[t[:, 1]], P[t[:, 2]], col, k)
            return
        # Crowns: smooth normals (soft, rounded foliage) and darker
        # underneath, as light gets inside a real crown less.
        nrm = v / np.maximum(scale, 1e-3)
        nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
        shade = 0.68 + 0.32 * (v[:, 2] + 1) / 2
        cc = np.asarray(col, np.float32)[None] * shade[:, None]
        m.add(P, nrm, cc, k, idx=t)

    trunk = hexrgb("#7a5a40")
    if kind == "pine":       # umbrella pine: tall bare trunk, wide flat crown
        put(CONE_V, CONE_T, np.array([0.35, 0.35, h * 0.8]), np.array([0, 0, 0]), trunk, WOOD)
        g = np.array(hexrgb("#4d7a3e")) * (0.9 + 0.2 * rnd.random())
        put(SPH_V, SPH_T, np.array([r, r, r * 0.38]), np.array([0, 0, h - r * 0.35]), g, LEAF, 0.08)
    elif kind == "cypress":
        put(CONE_V, CONE_T, np.array([0.25, 0.25, 1.5]), np.array([0, 0, 0]), trunk, WOOD)
        g = np.array(hexrgb("#3f6a3a")) * (0.9 + 0.2 * rnd.random())
        put(SPH_V, SPH_T, np.array([r, r, h * 0.48]), np.array([0, 0, h * 0.52]), g, LEAF, 0.05)
    elif kind == "palm":
        put(CONE_V, CONE_T, np.array([0.3, 0.3, h]), np.array([0, 0, 0]), hexrgb("#8b7356"), WOOD)
        g = np.array(hexrgb("#5f8c3f")) * (0.9 + 0.2 * rnd.random())
        for i in range(7):
            a = 2 * math.pi * i / 7 + rnd.random() * 0.4
            ca, sa = math.cos(a), math.sin(a)
            L = r * 1.1
            pts = np.array([[0, 0, h], [ca * L * 0.5 - sa * 0.5, sa * L * 0.5 + ca * 0.5, h + 0.5],
                            [ca * L, sa * L, h - 1.2], [ca * L * 0.5 + sa * 0.5, sa * L * 0.5 - ca * 0.5, h + 0.5]])
            P = enu_fast(x + pts[:, 0], y + pts[:, 1], z + pts[:, 2])
            for tri_ in ([0, 1, 2], [0, 2, 3], [0, 2, 1], [0, 3, 2]):
                m.tri(P[[tri_[0]]], P[[tri_[1]]], P[[tri_[2]]], g, LEAF)
    else:                    # broadleaf: two or three lumps
        put(CONE_V, CONE_T, np.array([0.3, 0.3, h * 0.55]), np.array([0, 0, 0]), trunk, WOOD)
        base_g = np.array(hexrgb(rnd.choice(["#6a9a45", "#7aa64f", "#5f8f40", "#86ad55", "#6e9c58",
                                             "#8aa848"])))
        cz = max(h - r * 0.9, r * 0.8)
        put(SPH_V, SPH_T, np.array([r, r, r * 0.85]), np.array([0, 0, cz]), base_g * (0.95 + 0.1 * rnd.random()), LEAF, 0.07)
        if r > 2.5:
            o = rnd.normal(0, r * 0.35, 2)
            put(SPH_V, SPH_T, np.array([r * 0.65] * 3), np.array([o[0], o[1], cz + r * 0.35]),
                base_g * (1.05 + 0.08 * rnd.random()), LEAF, 0.07)


def trees(m, mns, mnt, ortho, bmask, osm_pts, stats):
    chm = np.nan_to_num(mns - mnt, nan=0)
    chm[bmask > 0] = 0
    o = ortho.astype(np.float32)
    exg = 2 * o[..., 1] - o[..., 0] - o[..., 2]
    lum = o.mean(-1)
    veg = (chm > 2.5) & ((exg > 8) | (lum < 55))
    veg = ndimage.binary_opening(veg, iterations=1)
    lab, n = ndimage.label(veg)
    sizes = ndimage.sum(veg, lab, np.arange(1, n + 1))
    veg &= np.isin(lab, np.nonzero(sizes >= 12)[0] + 1)
    sm = ndimage.gaussian_filter(np.where(veg, chm, 0), 2.0)
    mx = ndimage.maximum_filter(sm, size=11)
    peaks = np.argwhere((sm == mx) & veg & (sm > 2.5))
    rnd = np.random.default_rng(7)
    placed = []
    for r, c in peaks:
        h = float(chm[max(0, r - 2):r + 3, max(0, c - 2):c + 3].max())
        h = min(h, 25)
        # Crown size: how far the canopy spreads around the peak.
        win = veg[max(0, r - 14):r + 15, max(0, c - 14):c + 15]
        rad = float(np.clip(math.sqrt(win.sum() * RES * RES / math.pi), 1.5, 6.5))
        x, y = cell_xy(r, c)
        z = mnt[r, c] if np.isfinite(mnt[r, c]) else 0
        rad *= 0.85                     # leave the streets some air
        if h > 14 and rad > 4.5 and rnd.random() < 0.6:
            kind = "pine"
        elif h > 8 and rad < 2.0:
            kind = "cypress"
        elif 6 < h < 16 and rad < 3 and rnd.random() < 0.35:
            kind = "palm"
        else:
            kind = "broad"
        add_tree(m, x, y, z, h, rad, kind, rnd)
        placed.append((x, y))
        stats["trees"] += 1
    placed = np.array(placed) if placed else np.zeros((0, 2))
    mntR = Raster(mnt)
    for (x, y), t in osm_pts:
        if t.get("natural") != "tree" or not (X0 < x < X1 and Y0 < y < Y1):
            continue
        if len(placed) and np.min(np.hypot(placed[:, 0] - x, placed[:, 1] - y)) < 4:
            continue
        z = float(mntR.sample(np.array([x]), np.array([y]))[0])
        if not np.isfinite(z):
            continue
        kind = "pine" if t.get("leaf_type") == "needleleaved" else "broad"
        add_tree(m, x, y, z, 9 if kind == "pine" else 7, 3.0, kind, rnd)
        stats["trees"] += 1


# ---------------------------------------------------------------- harbour ----
def hull_outline(L, W):
    """Yacht plan in (along, across) metres: square stern, parallel sides,
    a rounded bow coming to a point."""
    pts = []
    for u in np.linspace(0, 0.62, 4):
        pts.append((u * L, -W / 2 * (0.92 + 0.08 * min(u / 0.12, 1))))
    for k in np.linspace(0, 1, 8)[1:]:
        u = 0.62 + 0.38 * k
        pts.append((u * L, -W / 2 * math.sqrt(max(0.0, 1 - k ** 1.6))))
    right = [(u, -v) for u, v in reversed(pts[:-1])]
    return pts + right


def yachts(m, ortho, sea, stats):
    """Moored yachts traced from the white shapes on the water in the IGN
    photo (newer than the LiDAR), rebuilt as clean stylised yachts: white
    hull with a dark boot stripe, decks and stepped superstructures with
    dark window bands, bow pointing away from the quay."""
    land = ~sea
    dist_land = ndimage.distance_transform_edt(~land) * RES
    o = ortho.astype(np.float32)
    lum = o.mean(-1)
    sat = o.max(-1) - o.min(-1)
    boat = sea & (lum > 135) & (sat < 70)
    boat = ndimage.binary_opening(boat, iterations=1)
    boat = ndimage.binary_closing(boat, iterations=1)
    lab, n = ndimage.label(boat)
    rnd = np.random.default_rng(5)
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        rr, cc = np.nonzero(lab[sl] == i)
        if len(rr) < 40 or len(rr) > 60000:
            continue
        rr = rr + sl[0].start; cc = cc + sl[1].start
        x, y = cell_xy(rr, cc)
        pts = np.column_stack([x, y])
        cen = pts.mean(0)
        w, v = np.linalg.eigh(np.cov((pts - cen).T))
        ax = v[:, 1]
        proj = (pts - cen) @ ax
        perp = (pts - cen) @ v[:, 0]
        L = proj.max() - proj.min() + RES
        Wr = perp.max() - perp.min() + RES
        fill = len(rr) * RES * RES / max(L * Wr, 1)
        if L < 5 or Wr < 1.8 or fill < 0.45 or L / Wr > 7 or L > 140:
            continue                     # wakes, glints, foam
        W = min(Wr, L / 2.6)
        mid = cen + ax * (proj.max() + proj.min()) / 2

        def dl(p):
            c_ = int((p[0] - X0) / RES); r_ = int((Y1 - p[1]) / RES)
            return dist_land[np.clip(r_, 0, sea.shape[0] - 1), np.clip(c_, 0, sea.shape[1] - 1)]
        if dl(mid + ax * L / 2) < dl(mid - ax * L / 2):
            ax = -ax
        side = np.array([-ax[1], ax[0]])
        stern = mid - ax * L / 2

        def plan(along0, along1, half):
            q = [(stern + ax * a + side * s) for a, s in
                 [(along0, -half), (along1, -half), (along1, half), (along0, half)]]
            return orient(Polygon(q), 1.0)

        hull = orient(Polygon([stern + ax * u + side * s for u, s in hull_outline(L, W)]), 1.0)
        if not hull.is_valid:
            hull = hull.buffer(0)
        if not isinstance(hull, Polygon):
            continue
        hull_h = min(0.8 + 0.045 * L, 4.5)
        white = np.array(hexrgb("#f6f5f1"))
        stripe = np.array(hexrgb(rnd.choice(["#1f2a3a", "#1f2a3a", "#f6f5f1", "#6d7780", "#0e3a5c"])))
        add_walls(m, hull, -0.4, 0.35, -0.4, 100, stripe * 0.9, HULL)
        add_walls(m, hull, 0.35, hull_h, -0.4, 100, white, HULL)
        deck = hull.buffer(-0.25)
        add_roof(m, hull, hull_h, white * 0.97, False)
        if L > 9 and not deck.is_empty:
            aft = plan(0.03 * L, 0.2 * L, W * 0.42).intersection(deck)
            for q in to_polys(aft):
                add_roof(m, orient(q, 1.0), hull_h + 0.03, hexrgb("#b88a5c"), False)   # teak
        tiers = 1 if L < 14 else 2 if L < 30 else 3 if L < 55 else 4
        z = hull_h
        for t in range(tiers):
            a0, a1 = (0.2 + 0.05 * t) * L, (0.66 - 0.07 * t) * L
            half = W / 2 * (0.8 - 0.1 * t)
            if a1 - a0 < 2 or half < 0.6:
                break
            th = 2.4 if L > 14 else 1.5
            q = plan(a0, a1, half)
            add_walls(m, q, z, z + th, z, th, white, HULL)
            add_roof(m, q, z + th, white * 0.95, False)
            z += th
        stats["yachts"] += 1


def coast_land(ways, sea):
    """Raster (50 cm) of the land side of the OpenStreetMap coastline."""
    from shapely.ops import polygonize
    lines = [LineString(p) for t, p, c in ways if t.get("natural") == "coastline"]
    n = sea.shape[0]
    if not lines:
        return np.ones_like(sea)
    bb = box(X0 - 5, Y0 - 5, X1 + 5, Y1 + 5)
    img = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(img)
    for p in polygonize(unary_union([ln.intersection(bb) for ln in lines] + [bb.exterior])):
        ring = [((x - X0) / RES, (Y1 - y) / RES) for x, y in p.exterior.coords]
        mk = Image.new("L", (n, n), 0)
        ImageDraw.Draw(mk).polygon(ring, fill=1)
        mm = np.asarray(mk).astype(bool)
        if mm.any() and (~sea[mm]).mean() > 0.5:      # mostly land in the LiDAR too
            d.polygon(ring, fill=255)
    return np.asarray(img) > 0


def piers(m, ways, sea):
    seaR = Raster(sea.astype(np.float32))
    for t, pts, closed in ways:
        if t.get("man_made") not in ("pier", "breakwater") and t.get("floating") != "yes":
            continue
        g = Polygon(pts) if closed and len(pts) > 3 else LineString(pts).buffer(1.25, cap_style=2)
        g = g.intersection(box(X0, Y0, X1, Y1))
        for p in to_polys(g):
            c = p.representative_point()
            if seaR.sample(np.array([c.x]), np.array([c.y]))[0] < 0.5:
                continue      # on land already
            p = orient(p.simplify(0.3), 1.0)
            add_walls(m, p, -0.5, 0.9, -0.5, 10, hexrgb("#9a8a74"), PLAIN)
            add_roof(m, p, 0.9, hexrgb("#b9a487"), False)


def sea_plane(m):
    """A 40 km square of sea out to the horizon, centred on the district,
    bent to follow the earth's curve and kept 0.5 m above the globe's sea."""
    n, s = 33, 20000
    g = np.linspace(-s, s, n)
    X, Y = np.meshgrid(g, g)
    Z = GEOID_N + 0.5 - (X ** 2 + Y ** 2) / (2 * 6.371e6)
    P = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])
    ii = np.arange(n * n).reshape(n, n)
    a, b, c, d = ii[:-1, :-1], ii[:-1, 1:], ii[1:, :-1], ii[1:, 1:]
    idx = np.concatenate([np.stack([a, b, d], -1).reshape(-1, 3), np.stack([a, d, c], -1).reshape(-1, 3)])
    m.add(P, [0, 0, 1], hexrgb("#2d7a92"), WATER, uv=P[:, :2] / 10.0, idx=idx)


# --------------------------------------------------------------------- glb ----
def write_glb(path, prims, ground_jpg):
    """prims: list of (Mesh, textured). Positions are ENU; glTF is y-up."""
    bin_ = bytearray()
    views, accs, meshes_p = [], [], []

    def add_view(data, target=None):
        while len(bin_) % 4:
            bin_.append(0)
        views.append({"buffer": 0, "byteOffset": len(bin_), "byteLength": len(data),
                      **({"target": target} if target else {})})
        bin_.extend(data)
        return len(views) - 1

    def acc(arr, ctype, typ, target, normalized=False, minmax=False):
        v = add_view(arr.tobytes(), target)
        a = {"bufferView": v, "componentType": ctype, "count": len(arr), "type": typ}
        if normalized:
            a["normalized"] = True
        if minmax:
            a["min"] = arr.min(0).tolist(); a["max"] = arr.max(0).tolist()
        accs.append(a)
        return len(accs) - 1

    materials = [
        {"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "metallicFactor": 0, "roughnessFactor": 0.9}},
        {"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "baseColorTexture": {"index": 0},
                                  "metallicFactor": 0, "roughnessFactor": 0.95}},
    ]
    tex_view = add_view(ground_jpg)
    for mesh, textured in prims:
        arr = mesh.arrays()
        if arr is None:
            continue
        p, n, c, uv, i = arr
        yup = lambda q: np.column_stack([q[:, 0], q[:, 2], -q[:, 1]]).astype("<f4")
        attrs = {
            "POSITION": acc(yup(p), 5126, "VEC3", 34962, minmax=True),
            "NORMAL": acc(yup(n), 5126, "VEC3", 34962),
            "COLOR_0": acc(c.astype(np.uint8), 5121, "VEC4", 34962, normalized=True),
            "TEXCOORD_0": acc(uv.astype("<f4"), 5126, "VEC2", 34962),
        }
        idx = acc(i.astype("<u4"), 5125, "SCALAR", 34963)
        meshes_p.append({"attributes": attrs, "indices": idx, "material": 1 if textured else 0})
    gltf = {
        "asset": {"version": "2.0", "generator": "MC-3D build_filmset.py"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": meshes_p}],
        "materials": materials,
        "images": [{"bufferView": tex_view, "mimeType": "image/jpeg"}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "textures": [{"source": 0, "sampler": 0}],
        "buffers": [{"byteLength": len(bin_)}],
        "bufferViews": views, "accessors": accs,
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    while len(bin_) % 4:
        bin_.append(0)
    total = 12 + 8 + len(js) + 8 + len(bin_)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A)); f.write(js)
        f.write(struct.pack("<II", len(bin_), 0x004E4942)); f.write(bin_)


# ------------------------------------------------------------ drive map ----
# For driving the set yourself (viewer/drive.js): a 1 m grid in Lambert-93
# of the ground height the set is built on and what blocks a car. PNG, RGB:
# R/G = (height + 10 m) in cm, B = 0 road or ground, 1 building, 2 sea,
# 3 the tunnel's road (height is the road, not the deck over it).
def drive_map(gz, bmask, sea):
    n = int(X1 - X0)
    cx = np.arange(n) + 0.5
    YY, XX = np.meshgrid(cx, cx, indexing="ij")          # rows from the top (Y1), cols from X0
    h = ndimage.map_coordinates(gz, [YY / GRID, XX / GRID], order=1, mode="nearest")
    def down(mask):
        m_ = mask[: 2 * n, : 2 * n]
        return m_.reshape(n, 2, n, 2).mean((1, 3))
    flag = np.zeros((n, n), np.uint8)
    flag[down(sea.astype(np.float32)) > 0.5] = 2
    flag[down(bmask.astype(np.float32)) > 0.4] = 1
    if TUNNEL is not None:
        t = TUNNEL
        X, Y = X0 + XX, Y1 - YY
        def line(a, b):
            sel = (t["s"] >= a) & (t["s"] <= b)
            return LineString(np.column_stack([t["x"][sel], t["y"][sel]]))
        tube = line(t["ta"] - 1, t["tb"] + 1).buffer(TUBE_HALF - 0.6, cap_style=2)
        road = unary_union([line(t["ta"] - 14, t["ta"]).buffer(5.5, cap_style=2),
                            line(t["tb"], t["tb"] + 14).buffer(5.5, cap_style=2)])
        flag[contains_xy(t["ground"], X, Y)] = 1
        flag[contains_xy(road, X, Y)] = 0
        inside = contains_xy(tube, X, Y)
        from scipy.spatial import cKDTree
        _, j = cKDTree(np.column_stack([t["x"], t["y"]])).query(np.column_stack([X[inside], Y[inside]]))
        h[inside] = t["z"][j]
        flag[inside] = 3
    v = np.clip(np.round((h + 10) * 100), 0, 65535).astype(np.uint32)
    img = np.dstack([(v >> 8).astype(np.uint8), (v & 255).astype(np.uint8), flag])
    Image.fromarray(img, "RGB").save(OUT / "monte-carlo-drive.png", optimize=True)
    return {"file": "monte-carlo-drive.png", "x0": X0, "y1": Y1, "res": 1.0, "size": n,
            "affine": [[round(float(v_), 9) for v_ in row] for row in AFF]}


# ------------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="download the inputs first")
    ap.add_argument("--no-compress", action="store_true", help="skip meshopt compression")
    args = ap.parse_args()
    if args.fetch:
        fetch()
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    mnt, mns = load_raster("mnt.tif"), load_raster("mns.tif")
    ortho = np.asarray(Image.open(RAW / "ortho.jpg").convert("RGB"))
    # Sea: no terrain in the LiDAR, or ground at sea level.
    # Moored yachts and pontoons show up as small islands of "ground" in the
    # LiDAR terrain model; anything smaller than 0.2 ha surrounded by water
    # is sea (yachts are rebuilt below, OSM piers become pontoons).
    sea = np.isnan(mnt) | (mnt < 0.6)
    lab, n = ndimage.label(~sea)
    sizes = ndimage.sum(~sea, lab, np.arange(1, n + 1))
    sea |= np.isin(lab, np.nonzero(sizes < 8000)[0] + 1)
    sea = ndimage.binary_opening(sea, iterations=2)
    ways, pts = load_osm()
    # Yachts moored against a quay merge with it in the LiDAR terrain. The
    # OpenStreetMap coastline follows the quays, so anything outside it is sea.
    sea |= ~coast_land(ways, sea)
    mnt_f = np.where(sea, np.nan, mnt)
    global TUNNEL
    TUNNEL = load_tunnel()
    mnt_ground = mnt_f
    if TUNNEL is not None:
        timg = Image.new("L", (mnt.shape[1], mnt.shape[0]), 0)
        ImageDraw.Draw(timg).polygon([((x - X0) / RES, (Y1 - y) / RES) for x, y in TUNNEL["ground"].exterior.coords],
                                     fill=255)
        tmask = np.asarray(timg) > 0
        mnt_f = np.where(tmask & np.isfinite(mnt_f), np.maximum(mnt_f, TUNNEL["top"]), mnt_f)
    feats = json.load(open(RAW / "bdtopo.json"))["features"]
    stats = {"buildings": 0, "dropped": 0, "rooftop": 0, "roof_plants": 0, "levels": 0, "towers": 0, "trees": 0, "yachts": 0,
             "real_floors": 0, "styles": [0] * 9}

    print("buildings…")
    bm = Mesh()
    fps = buildings(bm, feats, mns, mnt_f, ortho, stats, FacadeData(ways))
    # Building mask at 50 cm for trees and the ground shade.
    mimg = Image.new("L", (mnt.shape[1], mnt.shape[0]), 0)
    dd = ImageDraw.Draw(mimg)
    for p in fps:
        dd.polygon([((x - X0) / RES, (Y1 - y) / RES) for x, y in p.exterior.coords], fill=255)
    bmask = np.asarray(mimg) > 0
    bgrow = ndimage.binary_dilation(bmask, iterations=3)

    # The tunnel tube is a file of its own: the viewer loads it without
    # shadows (a closed tube would only show shadow-map stairs inside).
    tun_m = Mesh()
    if TUNNEL is not None:
        tunnel_tube(tun_m)
    print("trees…")
    tm = Mesh()
    trees(tm, mns, mnt_f, ortho, bgrow.astype(np.uint8), pts, stats)
    print("harbour…")
    hm = Mesh()
    yachts(hm, ortho, sea, stats)
    piers(hm, ways, sea)
    sm = Mesh()
    sea_plane(sm)

    print("ground…")
    chm = np.nan_to_num(mns - mnt_f, nan=0)
    canopy = ((chm > 2.5) & ~bgrow).astype(np.float32)
    canopy = ndimage.zoom(canopy, 0.5, order=1)
    gm, gz = ground_mesh(mnt_ground, sea)
    drive = drive_map(gz, bmask, sea)
    jpg = paint_ground(ways, mnt_f, ndimage.zoom(sea.astype(np.float32), 0.5, order=1),
                       ndimage.zoom(bmask.astype(np.float32), 0.5, order=1), canopy, ortho,
                       sun_shadows(mns, sea))

    if tun_m.p:
        tiny = io.BytesIO()
        Image.new("RGB", (4, 4), (128, 128, 128)).save(tiny, "JPEG")
        write_glb(OUT / "monte-carlo-tunnel.glb", [(tun_m, False)], tiny.getvalue())
    raw = OUT / "monte-carlo.raw.glb"
    write_glb(raw, [(gm, True), (bm, False), (tm, False), (hm, False), (sm, False)], jpg)
    out = OUT / "monte-carlo.glb"
    if args.no_compress:
        raw.replace(out)
    else:
        # Draco: decoded by Cesium in a web worker; 16-bit positions keep
        # about 2 cm precision over the district.
        subprocess.run(["npx", "--yes", "@gltf-transform/cli@4.1.1", "meshopt", str(raw), str(out),
                        "--quantize-position", "16", "--quantize-texcoord", "14",
                        "--quantize-normal", "8"], check=True)
        raw.unlink()
    tris = sum(len(np.concatenate(x.i)) // 3 for x in (gm, bm, tm, hm, sm) if x.i)
    meta = {
        "name": "Monte-Carlo film set",
        "origin": {"lon": ORIGIN[0], "lat": ORIGIN[1], "height": 0},
        "bounds_l93": [X0, Y0, X1, Y1],
        "clip": [[round(float(v[0]), 7) for v in to_lonlat(np.array([x]), np.array([y]))]
                 for x, y in [(X0, Y0), (X1, Y0), (X1, Y1), (X0, Y1)]],
        "triangles": int(tris),
        "tunnel": bool(tun_m.p),
        "drive": drive,
        **stats,
        "sources": ["IGN LiDAR HD MNT/MNS (Etalab 2.0)", "IGN BD TOPO (Etalab 2.0)",
                    "IGN BD ORTHO, classification only (Etalab 2.0)", "OpenStreetMap contributors (ODbL)",
                    "Wikidata (CC0)"],
    }
    (OUT / "monte-carlo.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta), f"{time.time() - t0:.0f}s", f"{out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
