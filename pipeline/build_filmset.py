#!/usr/bin/env python3
"""Build our own stylised 3D model of Monte-Carlo, like an animated-film set.

Nothing here comes from Google: every shape is derived from open data and
every colour is authored by us, except the walls facing the circuit, whose
colours are sampled from Mapillary street photos (facade_colours.py, cached
in pipeline/facade_colours.json).

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
  Mareterra  The 2024 land extension, absent from the 2021 LiDAR: rebuilt
             from OpenStreetMap (see mareterra_setup and friends).

Output: viewer/filmset/monte-carlo.glb (y-up glTF in an east-north-up frame
centred on Casino square, heights in metres above the ellipsoid minus the
geoid offset handled by the viewer) and viewer/filmset/monte-carlo.json.

Inputs (downloaded by --fetch into data/raw/filmset/):
  mnt.tif, mns.tif   IGN LiDAR HD 50 cm via Géoplateforme WMS
  ortho.jpg          IGN orthophoto 50 cm (classifies roofs, trees, yachts)
  ortho20_XY.jpg     IGN orthophoto 20 cm, four tiles: the real roofs (each
                     roof's patch is shifted back by the photo's lean)
  bdtopo.json        IGN BD TOPO building outlines
  osm.xml            OpenStreetMap extract (api.openstreetmap.org /map)
  facade_colours.json (committed, next to this script) wall colours by
                     BD TOPO cleabs, from Mapillary (CC-BY-SA 4.0)

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
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from scipy import ndimage
from shapely import contains_xy
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box, shape
from shapely.geometry.polygon import orient
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "filmset"
OUT = ROOT / "viewer" / "filmset"

# District: Monte-Carlo, Casino square, Port Hercule and the Larvotto edge:
# the whole Grand Prix circuit. Lambert-93 metres (a square).
X0, Y0, X1, Y1 = 1055700, 6302500, 1057200, 6304000
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
    # The roofs, at about 20 cm (the photo's own resolution): four tiles of
    # 3840 px, PNG so the WMS's JPEG blocks do not land on the roofs.
    half = (X1 - X0) / 2
    for i in (0, 1):
        for j in (0, 1):
            bx, by = X0 + half * i, Y0 + half * j
            urls[f"ortho20_{i}{j}.png"] = (
                f"{WMS}?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ORTHOIMAGERY.ORTHOPHOTOS"
                f"&STYLES=&FORMAT=image/png&CRS=EPSG:2154&BBOX={bx},{by},{bx + half},{by + half}"
                f"&WIDTH=3840&HEIGHT=3840")
    for name, url in urls.items():
        out = RAW / name
        if out.exists() and out.stat().st_size > 1000:
            continue
        print("fetch", name)
        req = urllib.request.Request(url, headers={"User-Agent": "MC-3D film set builder"})
        out.write_bytes(urllib.request.urlopen(req, timeout=300).read())
        time.sleep(1.2)


def load_ortho_hi():
    """The four 20 cm photo tiles as one image (north up), or None."""
    tiles = [[RAW / f"ortho20_{i}{j}.png" for j in (0, 1)] for i in (0, 1)]
    if not all(t.exists() for row in tiles for t in row):
        tiles = [[RAW / f"ortho20_{i}{j}.jpg" for j in (0, 1)] for i in (0, 1)]
    if not all(t.exists() for row in tiles for t in row):
        return None
    k = Image.open(tiles[0][0]).size[0]
    im = Image.new("RGB", (2 * k, 2 * k))
    for i in (0, 1):
        for j in (0, 1):
            im.paste(Image.open(tiles[i][j]).convert("RGB"), (k * i, k * (1 - j)))
    return im


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
GROUND, WALL_OLD, WALL_NEW, ROOF_FLAT, ROOF_TILE, LEAF, WOOD, HULL, WATER, PLAIN, WALL_GLASS, ROAD = range(12)


class Mesh:
    def __init__(self):
        self.p, self.n, self.c, self.uv, self.i = [], [], [], [], []
        self.uv2 = []          # second texture coordinate (roads: across/along)
        self.count = 0

    def add(self, pos, nrm, col, kind, uv=None, idx=None, style=None, uv2=None):
        pos = np.asarray(pos, np.float32)
        k = len(pos)
        nrm = np.broadcast_to(np.asarray(nrm, np.float32), (k, 3))
        col = np.broadcast_to(np.asarray(col, np.float32), (k, 3))
        rgba = np.column_stack([np.clip(col, 0, 255), np.full(k, kind * 16 + (8 if style is None else style))]).astype(np.uint8)
        uv = np.zeros((k, 2), np.float32) if uv is None else np.asarray(uv, np.float32)
        idx = np.arange(k) if idx is None else np.asarray(idx)
        self.p.append(pos); self.n.append(nrm); self.c.append(rgba); self.uv.append(uv)
        self.uv2.append(np.zeros((k, 2), np.float32) if uv2 is None else np.asarray(uv2, np.float32))
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


def harbour_shadow_surface(mns, mnt, bgrow):
    """The surface that casts the baked shadows, without the Grand Prix
    grandstands and cabins the 2021 LiDAR caught on the harbour quays
    (they cast big dark blotches over quays that are open in the film
    set): along the harbour stretch, only buildings stand above ground."""
    if TRACK is None:
        return mns
    t = TRACK
    n = len(t["x"])
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    s_ = np.arange(n) * step
    tb = TUNNEL["tb"] if TUNNEL is not None else -1
    sel = (t["z"] < 8.0) & (s_ > tb + 30)
    if sel.sum() < 10:
        return mns
    from scipy.spatial import cKDTree
    kd = cKDTree(np.column_stack([t["x"][sel], t["y"][sel]]))
    rows, cols = np.mgrid[0:mns.shape[0], 0:mns.shape[1]]
    cx, cy = cell_xy(rows.ravel(), cols.ravel())
    d, _ = kd.query(np.column_stack([cx, cy]), distance_upper_bound=60)
    zone = np.isfinite(d).reshape(mns.shape) & ~bgrow
    return np.where(zone & np.isfinite(mnt), mnt, mns)


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
    if TRACK is not None:
        # The track: ground just under the modelled road, below the barrier
        # tops for one more grid diagonal (so no ground triangle reaches
        # over a barrier), then rising at most 1.5 m per metre: slopes
        # never spill over the track's edges.
        d = track_dist(X.ravel(), Y.ravel()).reshape(X.shape)
        band = d < TRACK_HALF + 15
        if TUNNEL is not None:
            band &= ~contains_xy(TUNNEL["ground"].buffer(3), X, Y) & ~contains_xy(TUNNEL["approach"], X, Y)
        if band.any():
            # Every stretch of the lap within reach counts, not only the
            # nearest: below the hairpin two legs run 14 m apart and 6 m
            # apart in height, and the upper one must not lift the ground
            # beside the lower one.
            dk, jk = TRACK["kd"].query(np.column_stack([X[band], Y[band]]), k=16,
                                       distance_upper_bound=TRACK_HALF + 16)
            ok = np.isfinite(dk)
            jk = np.where(ok, jk, 0)
            zk = TRACK["z"][jk]
            zb = Z[band]
            # Flat under the road and one grid step past the barriers (a
            # ground triangle from there cannot poke up through the road).
            inner_k = ok & (dk < TRACK_HALF + 3.2)
            # 30 cm under the road: the road's edges sit 14 cm under the
            # lap's height, and ground 15 cm under it z-fought with them.
            # Past the flat band the ground rises gently for two metres
            # (a ground triangle reaching back over the road's edge stays
            # under it), then at most 1.5 m per metre.
            cap_k = np.where(ok, zk - GROUND_UNDER + 0.35 * np.clip(dk - (TRACK_HALF + 3.2), 0, 2.3)
                             + 1.5 * np.maximum(0, dk - (TRACK_HALF + 5.5)), np.inf)
            # Points of the nearest point's own stretch (within 24 m along
            # the lap) count at the nearest point's height: on a climb
            # (Avenue d'Ostende, 7 %) the lowest of them is the road 16 m
            # further down, and the ground sank a metre under the road
            # (the pavements stood up as steps). Another leg of the lap
            # within reach counts at its own height when lower (below the
            # Fairmont hairpin the legs run 14 m apart and up to 8 m apart
            # in height).
            nlap = len(TRACK["x"])
            gap = np.abs(jk - jk[:, :1])
            same = ok & (np.minimum(gap, nlap - gap) <= 12)
            near = np.where(inner_k[:, 0], zk[:, 0] - GROUND_UNDER, np.inf)
            other = np.where(inner_k & ~same, zk - GROUND_UNDER, np.inf).min(1)
            flat = np.minimum(np.where(inner_k & same, near[:, None], np.inf).min(1), other)
            inner = np.isfinite(flat)
            cap = cap_k.min(1)
            # Under a road the cap only comes from its own leg and legs
            # about level with it: at the Fairmont hairpin the way out runs
            # 15 m away and up to 8 m lower, and its cap pulled the ground
            # under the way in 2 m down; the road followed it in 3 m
            # steps and the ground's slopes showed through as shards.
            near_z = np.where(inner_k[:, 0], zk[:, 0], np.inf)
            near_z = np.where(np.isfinite(near_z), near_z, np.where(inner_k, zk, -np.inf).max(1))
            cap_in = np.where(ok & (zk > near_z[:, None] - 2.0), cap_k, np.inf).min(1)
            Z[band] = np.where(inner, np.minimum(flat, cap_in), np.minimum(zb, cap))
            if PROM is not None:
                # Avenue d'Ostende: the ground stays under the pavements,
                # out to the parapet on the sea side and 7 m past the edge
                # under the buildings on the other.
                xb, yb = X[band], Y[band]
                d1, j1 = TRACK["kd"].query(np.column_stack([xb, yb]))
                on = (j1 >= PROM["i0"]) & (j1 < PROM["i1"])
                k_ = np.clip(j1 - PROM["i0"], 0, len(PROM["idx"]) - 1)
                sea_side = ((xb - TRACK["x"][j1]) * PROM["nx"][k_] + (yb - TRACK["y"][j1]) * PROM["ny"][k_]) > 0
                lim = np.where(sea_side, PROM["pd"][k_] + 0.3, TRACK_HALF + 7.0)
                low = on & (d1 < lim)
                zz = Z[band]
                zz[low] = np.minimum(zz[low], TRACK["z"][j1[low]] - 0.1)
                # Past the parapet, down to the foot of its wall.
                past = on & sea_side & (d1 >= lim) & (d1 < lim + 8)
                hm_ = PROM["hmin"][r[band][past], c[band][past]]
                zz[past] = np.minimum(zz[past], np.maximum(hm_, TRACK["z"][j1[past]] - PROM_WALL) + 0.3)
                Z[band] = zz
    if PROM is not None:
        # Below Avenue d'Ostende the LiDAR ground between the buildings on
        # the slope (steps, walls, courtyards) turns into needles on the
        # 3 m grid: level any node more than 2.5 m above the median of
        # its neighbours.
        xs_, ys_ = TRACK["x"][PROM["idx"]], TRACK["y"][PROM["idx"]]
        zone = (X > xs_.min() - 70) & (X < xs_.max() + 70) & (Y > ys_.min() - 70) & (Y < ys_.max() + 70)
        d2, j2 = TRACK["kd"].query(np.column_stack([X[zone], Y[zone]]))
        k2 = np.clip(j2 - PROM["i0"], 0, len(PROM["idx"]) - 1)
        side2 = ((X[zone] - TRACK["x"][j2]) * PROM["nx"][k2] + (Y[zone] - TRACK["y"][j2]) * PROM["ny"][k2]) > 0
        sel = (j2 >= PROM["i0"]) & (j2 < PROM["i1"]) & side2 & (d2 > PROM["pd"][k2] + 1.0) & (d2 < 70)
        med = ndimage.median_filter(Z, 3)
        zz = Z[zone]
        mz = med[zone]
        spike = sel & (zz > mz + 2.5)
        zz[spike] = mz[spike]
        # Ground under the buildings on the slope is interpolated up to
        # their street doors: no node more than 3 m above the lowest
        # ground within 6 m.
        h2 = PROM["hmin2"][r[zone], c[zone]]
        zz[sel] = np.minimum(zz[sel], np.where(h2[sel] < 900, h2[sel] + 3.0, zz[sel]))
        Z[zone] = zz
    if TUNNEL is not None and "open_ground" in TUNNEL:
        # Outside the gallery the ground stays below the road, so the view
        # through the pillars is open to the shore.
        og = contains_xy(TUNNEL["open_ground"], X, Y) & (track_dist(X.ravel(), Y.ravel()).reshape(X.shape) < TUBE_HALF + 14)
        if og.any():
            from scipy.spatial import cKDTree
            _, j = cKDTree(np.column_stack([TUNNEL["x"], TUNNEL["y"]])).query(np.column_stack([X[og], Y[og]]))
            Z[og] = np.minimum(Z[og], TUNNEL["z"][j] - 1.2)
    Z, Zs = quai_ground(X, Y, Z)
    Z, Zs = sidewalk_ground(X, Y, Z, Zs)
    land = valid[r, c] > 0.5
    # A lone land node among sea nodes (the end of a jetty, a quay corner)
    # would stand up from the sea floor as a needle: leave it to the sea.
    nb = ndimage.convolve(land.astype(np.int32), np.ones((3, 3), np.int32), mode="nearest") - land
    land &= nb >= 3
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
    fine = []
    for zx0, zy0, zx1, zy1 in DETAIL_ZONES:
        # A 1 m grid over the zone instead of the 3 m one: the ravine walls
        # keep their real shape (ledges, overhangs smoothed, the gully).
        j0, j1 = int(math.ceil((zx0 - X0) / GRID)), int(math.floor((zx1 - X0) / GRID))
        i0, i1 = int(math.ceil((Y1 - zy1) / GRID)), int(math.floor((Y1 - zy0) / GRID))
        if j1 - j0 < 2 or i1 - i0 < 2:
            continue
        keep[i0:i1, j0:j1] = False
        fine.append(_detail_patch(mnt, Z, i0, i1, j0, j1))
    tris = np.concatenate([np.stack([a, cc, b], -1)[keep], np.stack([b, cc, d], -1)[keep]])
    m = Mesh()
    m.add(P, N, [255, 255, 255], GROUND, uv=uv, idx=tris)
    for Pf, Nf, uvf, tf in fine:
        m.add(Pf, Nf, [255, 255, 255], GROUND, uv=uvf, idx=tf)
    # What stands on the ground (roads, lamps, the drive map) stands on
    # the esplanade's slab where there is one.
    return m, np.where(land, Zs, SEA_FLOOR)


# Places where the 3 m ground grid is too coarse for the terrain: the
# Ravin de Sainte-Dévote (cliffs behind the church, under the bridges).
DETAIL_ZONES = [(1055925.0, 6303100.0, 1056140.0, 6303275.0)]
DETAIL_K = 3                         # 3 m / 3 = 1 m


def _detail_patch(mnt, Z, i0, i1, j0, j1):
    """A finer ground grid over coarse cells [i0, i1) x [j0, j1). Its edge
    nodes follow the coarse grid exactly (so there is no crack), and the
    finer LiDAR ground fades in over 6 m from the edge. Near the circuit
    it keeps the coarse heights, which the track rules have set."""
    K = DETAIL_K
    st = GRID / K
    nxf, nyf = (j1 - j0) * K + 1, (i1 - i0) * K + 1
    xs = X0 + j0 * GRID + np.arange(nxf) * st
    ys = Y1 - i0 * GRID - np.arange(nyf) * st
    Xf, Yf = np.meshgrid(xs, ys)
    # Coarse surface, bilinear.
    fi = (Y1 - Yf) / GRID
    fj = (Xf - X0) / GRID
    ia = np.clip(np.floor(fi).astype(int), 0, Z.shape[0] - 2)
    ja = np.clip(np.floor(fj).astype(int), 0, Z.shape[1] - 2)
    ti, tj = fi - ia, fj - ja
    Zc = (Z[ia, ja] * (1 - ti) * (1 - tj) + Z[ia, ja + 1] * (1 - ti) * tj +
          Z[ia + 1, ja] * ti * (1 - tj) + Z[ia + 1, ja + 1] * ti * tj)
    # Fine LiDAR ground: mean of the 50 cm cells within 75 cm.
    h = ndimage.uniform_filter(np.nan_to_num(mnt, nan=0.0), size=3)
    w = ndimage.uniform_filter((~np.isnan(mnt)).astype(np.float32), size=3)
    hm = np.where(w > 0.3, h / np.maximum(w, 1e-6), np.nan)
    r = np.clip(((Y1 - Yf) / RES).astype(int), 0, mnt.shape[0] - 1)
    c = np.clip(((Xf - X0) / RES).astype(int), 0, mnt.shape[1] - 1)
    Zm = hm[r, c]
    edge = np.minimum.reduce([Xf - xs[0], xs[-1] - Xf, ys[0] - Yf, Yf - ys[-1]])
    wt = np.clip(edge / 6.0, 0.0, 1.0)
    if TRACK is not None:
        dtr = track_dist(Xf.ravel(), Yf.ravel()).reshape(Xf.shape)
        wt *= np.clip((dtr - (TRACK_HALF + 16)) / 6.0, 0.0, 1.0)
    wt = np.where(np.isfinite(Zm) & (Zc > SEA_FLOOR + 1), wt, 0.0)
    Zf = Zc + wt * (np.nan_to_num(Zm, nan=0.0) - Zc)
    P = enu_fast(Xf.ravel(), Yf.ravel(), Zf.ravel())
    gy, gx = np.gradient(Zf, st)
    N = np.column_stack([-gx.ravel(), gy.ravel(), np.ones(Zf.size)])
    N /= np.linalg.norm(N, axis=1, keepdims=True)
    uv = np.column_stack([(Xf.ravel() - X0) / (X1 - X0), (Y1 - Yf.ravel()) / (Y1 - Y0)])
    ii = np.arange(nxf * nyf).reshape(nyf, nxf)
    a, b, cc, d = ii[:-1, :-1], ii[:-1, 1:], ii[1:, :-1], ii[1:, 1:]
    tris = np.concatenate([np.stack([a, cc, b], -1).reshape(-1, 3), np.stack([b, cc, d], -1).reshape(-1, 3)])
    return P, N, uv, tris


def race_pool(d, xy, pts, px):
    """A public racing pool seen from above: white coping, bright water
    shading darker towards the middle, dark lane lines with T ends and
    lane ropes."""
    g = Polygon(pts).buffer(0)
    rect = g.minimum_rotated_rectangle
    c = np.asarray(rect.exterior.coords)[:4]
    e1, e2 = c[1] - c[0], c[2] - c[1]
    if np.linalg.norm(e1) < np.linalg.norm(e2):
        c = np.roll(c, -1, axis=0)
        e1, e2 = c[1] - c[0], c[2] - c[1]
    L, W = np.linalg.norm(e1), np.linalg.norm(e2)
    u, v = e1 / L, e2 / W
    d.polygon(xy(np.asarray(g.buffer(1.2, join_style=2).exterior.coords)), fill=(236, 233, 224))
    for k, col in enumerate([(46, 132, 150), (40, 120, 140), (35, 108, 130)]):
        q = g.buffer(-0.6 * k, join_style=2)
        if not q.is_empty and isinstance(q, Polygon):
            d.polygon(xy(np.asarray(q.exterior.coords)), fill=col)
    lanes = max(4, min(10, int(W / 2.5)))
    lw = max(1, int(0.25 * px))
    for k in range(lanes):
        o = (k + 0.5) * W / lanes
        a, b = c[0] + v * o + u * 2.0, c[0] + v * o + u * (L - 2.0)
        d.line(xy([a, b]), fill=(18, 56, 96), width=lw)
        for e in (a, b):
            d.line(xy([e - v * 0.5, e + v * 0.5]), fill=(18, 56, 96), width=lw)
    for k in range(1, lanes):
        o = k * W / lanes
        a, b = c[0] + v * o, c[0] + v * o + u * L
        n_ = int(L / 0.6)
        for j in range(n_):
            p0, p1 = a + (b - a) * j / n_, a + (b - a) * (j + 0.5) / n_
            col = (220, 40, 40) if j < n_ * 0.1 or j > n_ * 0.9 else ((240, 240, 240) if j % 2 else (30, 90, 200))
            d.line(xy([p0, p1]), fill=col, width=max(1, int(0.12 * px)))

def clean_roof_patch(patch, pm, px):
    """One roof's piece of aerial photo, tidied for the set: the band along
    the outline (where the photo's lean and the trace leave a sliver of
    street, wall or the neighbour's roof) takes the roof's own colour from
    just inside it, and stains and hard shadows are toned down, so the roof
    reads as one clean surface with a crisp edge."""
    a = np.asarray(patch).astype(np.float32)
    m = np.asarray(pm) > 0
    if m.sum() < 30:
        return patch
    band = max(2, int(round(0.9 * px)))
    inner = ndimage.binary_erosion(m, iterations=band)
    if inner.sum() < 20:
        inner = ndimage.binary_erosion(m, iterations=1)
        if inner.sum() < 10:
            inner = m
    # Speckle out, then the roof's local colour (normalised blur over the
    # inner part only, so nothing from outside the outline leaks in).
    a = ndimage.median_filter(a, size=(3, 3, 1))
    w = inner.astype(np.float32)
    r = max(3.0, 2.0 * px)
    den = ndimage.gaussian_filter(w, r) + 1e-4
    local = np.stack([ndimage.gaussian_filter(a[..., k] * w, r) for k in range(3)], -1) / den[..., None]
    mean = a[inner].mean(0)
    local = np.where(den[..., None] > 0.02, local, mean)
    # Tame the contrast round the local colour (stains, tar patches,
    # shadows of the parapet), keeping roof kit and skylights readable.
    d = a - local
    # Dark blotches (tar, damp, the shadow of a parapet or a neighbour)
    # are pulled up harder than bright roof kit is pulled down.
    dark = np.clip(-d.mean(-1, keepdims=True) / 40.0, 0.0, 1.0)
    out = local + d * (0.6 - 0.25 * dark)
    # The edge band: the roof's own colour, faded in (half its overall
    # colour, so a shadowed rim does not become a dark frame).
    edge = 0.5 * local + 0.5 * mean
    dist = ndimage.distance_transform_edt(m)
    k = np.clip((dist - 1.0) / band, 0.0, 1.0)[..., None]
    out = edge * (1 - k) + out * k
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def paint_ground(ways, mnt, sea, bmask, canopy, ortho, shadow=None, roofs=None):
    S = 6144
    px = S / (X1 - X0)
    # Monaco is paved almost everywhere: pale stone, not sand.
    img = Image.new("RGB", (S, S), tuple(hexrgb("#cdc7bb")))
    # Steep ground with nothing on it is bare limestone (the Rock, the
    # cliffs under the Moyenne Corniche).
    gy_, gx_ = np.gradient(np.nan_to_num(mnt, nan=0.0), RES)
    steep = (np.hypot(gx_, gy_) > 0.9) & np.isfinite(mnt)
    st_ = Image.fromarray((steep * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR).filter(ImageFilter.GaussianBlur(4))
    img = Image.composite(Image.new("RGB", (S, S), tuple(hexrgb("#b9ae99"))), img, st_.point(lambda v: int(v * 0.8)))
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
    # Along the circuit, green comes only from mapped gardens: the photo's
    # green beside the road is tree shade, awnings and planters, and painted
    # as lawn it smeared the pavements.
    street = Image.new("L", (S, S), 0)
    if TRACK is not None:
        ds_ = ImageDraw.Draw(street)
        for q in to_polys(TRACK["line"].buffer(30.0)):
            ds_.polygon(xy(np.asarray(q.exterior.coords)), fill=255)
            for h in q.interiors:
                ds_.polygon(xy(np.asarray(h.coords)), fill=0)
        for t, pts, closed in ways:
            k = t.get("leisure") or t.get("landuse") or t.get("natural")
            if closed and k in GREEN and len(pts) >= 3:
                ds_.polygon(xy(pts), fill=0)
        street = street.filter(ImageFilter.GaussianBlur(4))
    keep_green = street.point(lambda v: 255 - v)
    # Lawns, planters and verges nobody mapped: green in the aerial photo
    # (low plants; the LiDAR canopy below handles trees).
    if ortho is not None:
        o = ortho.astype(np.float32)
        exg = 2 * o[..., 1] - o[..., 0] - o[..., 2]
        veg = (exg > 16) & (o.mean(2) < 170)
        veg = ndimage.binary_opening(veg, iterations=1)
        vi = Image.fromarray((veg * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR).filter(ImageFilter.GaussianBlur(2))
        vi = ImageChops.multiply(vi.point(lambda v: int(v * 0.85)), keep_green)
        img = Image.composite(Image.new("RGB", (S, S), tuple(hexrgb("#86a65a"))), img, vi)
        d = ImageDraw.Draw(img)
    # Ground under the LiDAR tree canopy: a darker garden green.
    can = Image.fromarray((canopy * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)
    can = can.filter(ImageFilter.GaussianBlur(3))
    img = Image.composite(Image.new("RGB", (S, S), tuple(hexrgb("#6d8a4c"))), img,
                          ImageChops.multiply(can.point(lambda v: int(min(255, v * 0.85))), keep_green))
    d = ImageDraw.Draw(img)
    # Squares and pedestrian areas.
    for t, pts, closed in ways:
        if closed and (t.get("area") == "yes" or t.get("area:highway")) and t.get("highway") in (
                "pedestrian", "footway", "service") or t.get("place") == "square" and closed:
            poly(pts, "#dfd9cd")
        if closed and t.get("amenity") == "parking" and t.get("parking") != "underground":
            poly(pts, "#9a9690")
    if QA is not None:
        # The quay at the foot of the wall: paving, not the lawn the
        # aerial photo's pines and awnings made of it.
        for q in to_polys(QA["poly"].buffer(8.0).difference(QA["poly"])):
            poly(np.asarray(q.exterior.coords), "#cfc9bd")
        # The Rainier III pool deck in pale stone (no lawn: the photo's
        # green there is sun loungers and the pool's glow).
        pools = [Polygon(p_).buffer(0) for t, p_, c in ways if c and len(p_) > 3 and t.get("leisure") == "swimming_pool"]
        for t, pts, closed in ways:
            if closed and len(pts) > 3 and t.get("leisure") in ("sports_centre", "stadium") and \
                    any(Polygon(pts).buffer(0).intersects(q) for q in pools) and Polygon(pts).buffer(0).distance(QA["poly"]) < 30:
                poly(pts, QA_DECK)
    # Roads: pavement first, asphalt on top, markings last.
    WIDTH = {"motorway": 12, "trunk": 11, "primary": 10, "secondary": 8.5, "tertiary": 7.5,
             "residential": 6, "unclassified": 6, "service": 4.5, "living_street": 5,
             "primary_link": 6, "secondary_link": 6, "tertiary_link": 5, "raceway": 9}
    roads = [(t, pts) for t, pts, closed in ways
             if t.get("highway") in WIDTH and t.get("tunnel") not in ("yes", "building_passage")
             and t.get("area") != "yes" and t.get("layer", "0") not in ("-1", "-2", "-3")
             and not t.get("bridge")]          # bridges get decks of their own
    for t, pts in roads:
        line(pts, "#dcd6ca", WIDTH[t["highway"]] + 4.5)
    for t, pts in roads:
        line(pts, "#cfc4b2", WIDTH[t["highway"]] + 1.2)       # kerb
    for t, pts in roads:
        line(pts, "#5d6068", WIDTH[t["highway"]])
    for t, pts, closed in ways:
        hw = t.get("highway")
        if hw in ("footway", "pedestrian", "path", "steps", "cycleway") and t.get("area") != "yes" \
                and t.get("tunnel") != "yes" and t.get("layer", "0") not in ("-1", "-2"):
            line(pts, "#cdbb9c" if hw == "steps" else "#e8dcc6", 3.0 if hw == "pedestrian" else 2.0)
    # Jetties and pontoons: pale concrete, as in the photo (their service
    # lanes are not asphalt roads).
    for t, pts, closed in ways:
        if t.get("man_made") in ("pier", "breakwater") or t.get("floating") == "yes":
            if closed and len(pts) > 3:
                poly(pts, "#cfcac0")
            elif len(pts) >= 2:
                line(pts, "#cfcac0", 2.6)
    if QA is not None:
        # Quai Albert 1er: the esplanade in grey asphalt, the walk under
        # the pines red-brown (over the footway lines drawn across it).
        for q in to_polys(QA["poly"]):
            poly(np.asarray(q.exterior.coords), QA_ASPHALT)
        for q in to_polys(QA["walk"]):
            poly(np.asarray(q.exterior.coords), QA_WALK)
    if SW is not None:
        # Pavements along the circuit: paving (what is in their holes,
        # gardens and planters, stays).
        mk_ = Image.new("L", img.size, 0)
        dm_ = ImageDraw.Draw(mk_)
        for q in to_polys(SW["poly"]):
            dm_.polygon(xy(np.asarray(q.exterior.coords)), fill=255)
            for h in q.interiors:
                dm_.polygon(xy(np.asarray(h.coords)), fill=0)
        for q in to_polys(SW.get("islands", Polygon())):
            dm_.polygon(xy(np.asarray(q.exterior.coords)), fill=255)
        img.paste(tuple(hexrgb(SW_PAVING)), (0, 0) + img.size, mk_)
        mk2 = Image.new("L", img.size, 0)
        dm2 = ImageDraw.Draw(mk2)
        for q in to_polys(SW["pale"]):
            dm2.polygon(xy(np.asarray(q.exterior.coords)), fill=255)
            for h in q.interiors:
                dm2.polygon(xy(np.asarray(h.coords)), fill=0)
        img.paste(tuple(hexrgb(SW_PALE)), (0, 0) + img.size, ImageChops.multiply(mk_, mk2))
        d = ImageDraw.Draw(img)
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
            if t.get("leisure") == "swimming_pool" and t.get("sport") == "swimming":
                race_pool(d, xy, pts, px)
            else:
                poly(pts, "#3e98a8")
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
    # Quays: a band of pale dressed stone (the coping) along the water's edge.
    sea_hard = seai > 0.5
    cop = ndimage.binary_dilation(sea_hard, iterations=max(2, int(0.9 * px))) & ~sea_hard
    arr[cop] = arr[cop] * 0.3 + np.array(hexrgb("#ebe6da"), np.float32) * 0.7
    arr = arr * (1 - seai[..., None]) + np.array(hexrgb("#2f6f86"))[None, None] * seai[..., None]
    # Paper-like grain so large areas do not look flat.
    g = np.random.default_rng(3).normal(0, 4, (S // 4, S // 4))
    g = np.asarray(Image.fromarray(g.astype(np.float32)).resize((S, S), Image.BILINEAR))
    arr += g[..., None]
    # Real roofs: the 20 cm aerial photo inside the outlines of the roofs
    # that use it (a little crisper and warmer than the raw photo).
    if roofs is not None and roofs[0] is not None and roofs[1]:
        hi, polys = roofs
        hi = hi.resize((S, S), Image.LANCZOS) if hi.size != (S, S) else hi
        mk = Image.new("L", (S, S), 0)
        canvas = Image.new("RGB", (S, S))
        # Lowest roofs first, so a taller neighbour wins along shared edges.
        for pg, (sx, sy), _z in sorted(polys, key=lambda t: t[2]):
            for q in to_polys(pg.buffer(0.6, join_style=2)):
                bx0, by0, bx1, by1 = q.bounds
                a0, b0 = int((bx0 - X0) * px) - 1, int((Y1 - by1) * px) - 1
                a1, b1 = int((bx1 - X0) * px) + 2, int((Y1 - by0) * px) + 2
                ox, oy = int(round(sx * px)), int(round(-sy * px))
                patch = hi.crop((a0 + ox, b0 + oy, a1 + ox, b1 + oy))
                pm = Image.new("L", (a1 - a0, b1 - b0), 0)
                dpm = ImageDraw.Draw(pm)
                dpm.polygon([(X - a0, Y - b0) for X, Y in xy(q.exterior.coords)], fill=255)
                for hole in q.interiors:
                    dpm.polygon([(X - a0, Y - b0) for X, Y in xy(hole.coords)], fill=0)
                canvas.paste(clean_roof_patch(patch, pm, px), (a0, b0), pm)
                mk.paste(255, (a0, b0, a1, b1), pm)
        mkf = np.asarray(mk).astype(np.float32)[..., None] / 255
        ph = np.asarray(canvas).astype(np.float32)
        gray = ph.mean(2, keepdims=True)
        ph = gray + (ph - gray) * 1.12
        ph = (ph - 118) * 1.1 + 126
        ph *= np.array([1.03, 1.0, 0.96], np.float32)
        arr = arr * (1 - mkf) + ph * mkf
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


BAY_NOW = None    # window spacing measured in the photos for the building being built
GROUND_R = None   # the terrain, while buildings() runs: street level along each wall
SHOP_NOW = None   # the real shops of the building being built (shop_spans)

# Shopfronts only where OpenStreetMap has a real shop, café, restaurant,
# bank or agency: most ground floors in Monte-Carlo are hotel entrances,
# office lobbies and flats. TEXCOORD_1.x on a wall = 1 where the ground
# floor is a shopfront, 0 for a plain residential / lobby ground floor.
SHOP_AMENITY = {"restaurant", "cafe", "bar", "pub", "fast_food", "ice_cream", "bank", "pharmacy",
                "bureau_de_change", "nightclub", "post_office", "car_rental", "driving_school",
                "veterinary", "marketplace", "atm_shop"}
SHOP_OFFICE = {"estate_agent", "insurance", "travel_agent"}     # agencies with a display window
SHOPS = None
SHOP_REACH = 6.0       # metres of frontage either side of each shop's point


def shop_places(ways, pts):
    """The real shops: points (L93) and areas (a shop mapped as a building)."""
    def is_shop(t):
        return (("shop" in t and t["shop"] not in ("no",)) or t.get("amenity") in SHOP_AMENITY
                or t.get("office") in SHOP_OFFICE or t.get("craft") in ("bakery", "confectionery"))
    P = [np.asarray(xy, float) for xy, t in pts if is_shop(t)]
    A = []
    for t, w, closed in ways:
        if closed and len(w) >= 4 and (is_shop(t) or t.get("building") == "retail"):
            g = Polygon(w).buffer(0)
            if g.area > 10:
                A.append(g)
    return {"pts": np.array(P).reshape(-1, 2), "areas": A}


def shops_for(poly, shops, others):
    """The shops of one building: points inside its outline, or just
    outside it (mapped on the pavement at the door) and in no other
    building; each with its distance to the nearest wall, so a shop
    fronts the street nearest it, not every side of the block."""
    if shops is None or not len(shops["pts"]):
        return None
    P = shops["pts"]
    x0, y0, x1, y1 = poly.bounds
    near = np.nonzero((P[:, 0] > x0 - 3) & (P[:, 0] < x1 + 3) & (P[:, 1] > y0 - 3) & (P[:, 1] < y1 + 3))[0]
    out = []
    for i in near:
        pt = Point(P[i])
        inside = poly.contains(pt)
        d = poly.exterior.distance(pt)
        if inside or (d < 3.0 and i not in others):
            out.append((P[i], d if inside else 0.0))
    areas = [a for a in shops["areas"] if a.intersection(poly).area > 0.5 * poly.area]
    if not out and not areas:
        return None
    return {"pts": out, "areas": areas}


def shop_spans(a, b):
    """Stretches (fractions 0-1, snapped to whole bays later) of the wall
    a-b with a real shop behind them, from SHOP_NOW."""
    if SHOP_NOW is None:
        return []
    if SHOP_NOW["areas"]:
        return [(0.0, 1.0)]
    L = float(np.linalg.norm(b - a))
    if L < 1.0:
        return []
    u = (b - a) / L
    out_ = np.array([u[1], -u[0]])          # walls are built CCW: outside on the right
    spans = []
    for p, dmin in SHOP_NOW["pts"]:
        d = p - a
        t, s = float(d @ u), float(d @ out_)
        # On this wall's side of the building: no deeper than the nearest
        # wall to it plus a little, and not far past either end.
        if -(dmin + 1.5) <= s <= 3.0 and -3.0 <= t <= L + 3.0:
            spans.append((max(0.0, (t - SHOP_REACH) / L), min(1.0, (t + SHOP_REACH) / L)))
    spans.sort()
    merged = []
    for s0, s1 in spans:
        if merged and s0 <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], s1))
        else:
            merged.append((s0, s1))
    return merged


def split_shops(t0, t1, nb, spans):
    """Cut [t0, t1] of a wall with nb bays into (t0, t1, shop) pieces at
    whole bays."""
    if not spans:
        return [(t0, t1, 0.0)]
    cuts = {t0, t1}
    for s0, s1 in spans:
        for s in (math.floor(s0 * nb) / nb, math.ceil(s1 * nb) / nb):
            if t0 < s < t1:
                cuts.add(s)
    cuts = sorted(cuts)
    res = []
    for c0, c1 in zip(cuts[:-1], cuts[1:]):
        mid = (c0 + c1) / 2
        res.append((c0, c1, 1.0 if any(math.floor(s0 * nb) / nb <= mid <= math.ceil(s1 * nb) / nb
                                       for s0, s1 in spans) else 0.0))
    return res


def street_rise(a, b, base, z0, z1, fh):
    """Where the street outside a wall climbs above the building's base (a
    building on a hill: its base is the lowest ground round it), the
    shops follow the street: the ground-floor row starts at the pavement
    all along the wall, and the floors above stay level. Returns the wall
    split into pieces (a, b, rise at a, rise at b, t at a, t at b, wall
    index), or None when the street
    is level with the base."""
    if GROUND_R is None or z0 > base + 0.5 or z1 - base < 2 * fh:
        return None
    L = np.linalg.norm(b - a, axis=1)
    u = (b - a) / np.maximum(L, 1e-9)[:, None]
    out = np.column_stack([u[:, 1], -u[:, 0]])
    res = []
    any_ = False
    for i in range(len(a)):
        n = max(1, int(math.ceil(L[i] / 3.0)))
        t = np.linspace(0, 1, n + 1)
        p = a[i] + t[:, None] * (b[i] - a[i])
        q = p + out[i] * 1.5
        g = GROUND_R.sample(q[:, 0], q[:, 1]) - base
        g = np.where(np.isfinite(g), g, 0.0)
        # A wall against a cliff or a retaining wall is not a street.
        g = np.where(g > 6.0, 0.0, np.clip(g, 0.0, None))
        g = np.minimum(g, z1 - base - fh - 0.5)
        if g.max() < 0.4:
            res.append((a[i], b[i], 0.0, 0.0, 0.0, 1.0, i))
            continue
        any_ = True
        for j in range(n):
            res.append((p[j], p[j + 1], float(g[j]), float(g[j + 1]), t[j], t[j + 1], i))
    return res if any_ else None


def add_walls(m, poly, z0, z1, base, fh, col, kind, inward=False, style=None, min_edge=0.3):
    poly = orient(poly, -1.0 if inward else 1.0)
    for ring in [poly.exterior] + list(poly.interiors):
        pts = np.asarray(ring.coords)[:, :2]
        if style is not None and not inward:
            a_, b_ = pts[:-1], pts[1:]
            okk = np.linalg.norm(b_ - a_, axis=1) > 0.3
            pieces = street_rise(a_[okk], b_[okk], base, z0, z1, fh)
            if pieces is not None:
                sloped_walls(m, pieces, a_[okk], b_[okk], z0, z1, base, fh, col, kind, style)
                continue
        a, b = pts[:-1], pts[1:]
        L = np.linalg.norm(b - a, axis=1)
        ok = L > min_edge
        a, b, L = a[ok], b[ok], L[ok]
        if not len(a):
            continue
        bay = BAY_NOW or STYLE_BAY[style] if style is not None else {WALL_OLD: 3.2, WALL_NEW: 3.4, WALL_GLASS: 2.2}.get(kind, 3.0)
        nb = np.maximum(1, np.round(L / bay))
        u0, u1, shop = np.zeros(len(a)), nb.copy(), np.zeros(len(a))
        if style is not None and not inward and SHOP_NOW is not None:
            # Walls with a real shop behind part of them: cut at whole bays
            # into shopfront and plain stretches.
            A_, B_, U0, U1, F = [], [], [], [], []
            for i in range(len(a)):
                for c0, c1, fl in split_shops(0.0, 1.0, nb[i], shop_spans(a[i], b[i])):
                    A_.append(a[i] + (b[i] - a[i]) * c0); B_.append(a[i] + (b[i] - a[i]) * c1)
                    U0.append(nb[i] * c0); U1.append(nb[i] * c1); F.append(fl)
            a, b = np.array(A_), np.array(B_)
            u0, u1, shop = np.array(U0), np.array(U1), np.array(F)
        k = len(a)
        P = enu_fast(np.column_stack([a[:, 0], b[:, 0], b[:, 0], a[:, 0]]).ravel(),
                     np.column_stack([a[:, 1], b[:, 1], b[:, 1], a[:, 1]]).ravel(),
                     np.tile([z0, z0, z1, z1], k)).reshape(k, 4, 3)
        nrm = np.cross(P[:, 1] - P[:, 0], P[:, 3] - P[:, 0])
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
        v0, v1 = (z0 - base) / fh, (z1 - base) / fh
        uv = np.stack([np.column_stack([u0, u1, u1, u0]),
                       np.column_stack([np.full(k, v0), np.full(k, v0), np.full(k, v1), np.full(k, v1)])], -1)
        idx = (np.arange(k)[:, None] * 4 + np.array([0, 1, 2, 0, 2, 3])[None]).ravel()
        uv2 = np.column_stack([np.repeat(shop, 4), np.zeros(4 * k)])
        m.add(P.reshape(-1, 3), np.repeat(nrm, 4, 0), col, kind, uv=uv.reshape(-1, 2), idx=idx, style=style,
              uv2=uv2)


def sloped_walls(m, pieces, A, B, z0, z1, base, fh, col, kind, style):
    """Walls whose shop floor climbs with the street (street_rise): per
    piece, the ground-floor band from the pavement up one storey, its
    window rows measured from the pavement, then the level floors above."""
    bay = BAY_NOW or STYLE_BAY[style]
    quads, uvs, flags = [], [], []
    spans = {}
    for (p, q, ga, gb, t0, t1, wi) in pieces:
        # The bay count of the whole wall the piece belongs to.
        L = float(np.linalg.norm(q - p) / max(t1 - t0, 1e-9))
        nb = max(1.0, round(L / bay))
        if wi not in spans:
            spans[wi] = shop_spans(A[wi], B[wi])
        tm = (t0 + t1) / 2
        fl = 1.0 if any(math.floor(s0 * nb) / nb <= tm <= math.ceil(s1 * nb) / nb for s0, s1 in spans[wi]) else 0.0
        u0, u1 = nb * t0, nb * t1
        ta, tb = base + ga + fh, base + gb + fh
        if ga < 0.01 and gb < 0.01:
            quads.append((p, q, (z0, z0, z1, z1)))
            uvs.append(((u0, (z0 - base) / fh), (u1, (z0 - base) / fh), (u1, (z1 - base) / fh), (u0, (z1 - base) / fh)))
            flags.append(fl)
            continue
        quads.append((p, q, (z0, z0, tb, ta)))
        uvs.append(((u0, (z0 - base - ga) / fh), (u1, (z0 - base - gb) / fh), (u1, 1.0), (u0, 1.0)))
        quads.append((p, q, (ta, tb, z1, z1)))
        uvs.append(((u0, (ta - base) / fh), (u1, (tb - base) / fh), (u1, (z1 - base) / fh), (u0, (z1 - base) / fh)))
        flags += [fl, fl]
    k = len(quads)
    xs = np.array([[p[0], q[0], q[0], p[0]] for p, q, _ in quads]).ravel()
    ys = np.array([[p[1], q[1], q[1], p[1]] for p, q, _ in quads]).ravel()
    zs = np.array([z for _, _, z in quads]).ravel()
    P = enu_fast(xs, ys, zs).reshape(k, 4, 3)
    # Normal from the piece's horizontal direction (the quads are not all
    # rectangles: the top of the shop band slopes).
    H = P[:, 1] - P[:, 0]
    Up = enu_fast(xs[:1], ys[:1], np.array([1.0]))[0] - enu_fast(xs[:1], ys[:1], np.array([0.0]))[0]
    nrm = np.cross(H, np.broadcast_to(Up, H.shape))
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
    idx = (np.arange(k)[:, None] * 4 + np.array([0, 1, 2, 0, 2, 3])[None]).ravel()
    uv2 = np.column_stack([np.repeat(np.array(flags), 4), np.zeros(4 * k)])
    m.add(P.reshape(-1, 3), np.repeat(nrm, 4, 0), col, kind, uv=np.array(uvs, np.float64).reshape(-1, 2),
          idx=idx, style=style, uv2=uv2)


# Roofs are painted with the aerial photo itself: real tiles, skylights,
# terraces. The IGN photo is not a true orthophoto, so a roof shows up
# shifted from its outline by its height times a lean (estimate_lean). The
# build copies each roof's patch of photo, shifted back, into the ground
# map under the building's own outline (ground nobody sees), and the roof
# samples the ground map there.
PHOTO_ROOF = 1                      # ROOF_FLAT style: diffuse from the ground map


def photo_edges(hi):
    """Edge strength of the 20 cm photo on the 50 cm grid."""
    n = int((X1 - X0) / RES)
    g = np.asarray(hi.convert("L").resize((n, n), Image.LANCZOS)).astype(np.float32)
    G = np.hypot(ndimage.sobel(g, 0), ndimage.sobel(g, 1))
    return G / (ndimage.uniform_filter(G, 15) + 10)


def outline_score(G, poly, R):
    """Mean photo edge strength along the outline, for every shift of up
    to R cells: (2R+1, 2R+1) array, rows = north shift down."""
    x0, y0, x1, y1 = poly.bounds
    c0, r0 = int((x0 - X0) / RES), int((Y1 - y1) / RES)
    w, h = int((x1 - X0) / RES) + 1 - c0, int((Y1 - y0) / RES) + 1 - r0
    im = Image.new("L", (w, h), 0)
    ImageDraw.Draw(im).polygon([((x - X0) / RES - c0, (Y1 - y) / RES - r0) for x, y in poly.exterior.coords], fill=1)
    m = np.asarray(im).astype(bool)
    ys, xs = np.nonzero(m ^ ndimage.binary_erosion(m))
    sc = np.zeros((2 * R + 1, 2 * R + 1), np.float32)
    if not len(ys):
        return sc
    n = G.shape[0]
    for dy in range(-R, R + 1):
        yy = ys + r0 + dy
        for dx in range(-R, R + 1):
            xx = xs + c0 + dx
            ok = (yy >= 0) & (yy < n) & (xx >= 0) & (xx < n)
            sc[dy + R, dx + R] = G[yy[ok], xx[ok]].mean() if ok.any() else 0
    return sc


def estimate_lean(G, feats, mns, mnt):
    """Roof shift per metre of height (east, north): the one that best
    lines up the outlines of the taller buildings with edges in the photo."""
    R = 30
    hs, maps = [], []
    for f in feats:
        for p in to_polys(shapely.force_2d(shape(f["geometry"])).buffer(0)):
            x0, y0, x1, y1 = p.bounds
            if p.area < 200 or x0 < X0 + 20 or x1 > X1 - 20 or y0 < Y0 + 20 or y1 > Y1 - 20:
                continue
            c0, c1 = int((x0 - X0) / RES), int((x1 - X0) / RES) + 1
            r0, r1 = int((Y1 - y1) / RES), int((Y1 - y0) / RES) + 1
            gx, gy = cell_xy(np.arange(r0, r1)[:, None] + 0.5, np.arange(c0, c1)[None] + 0.5)
            ins = contains_xy(p, *np.broadcast_arrays(gx, gy))
            top, gnd = mns[r0:r1, c0:c1][ins], mnt[r0:r1, c0:c1][ins]
            if ins.sum() < 50 or not np.isfinite(gnd).any() or not np.isfinite(top).any():
                continue
            H = float(np.nanpercentile(top, 92) - np.nanmin(gnd))
            if H < 12:
                continue
            sc = outline_score(G, p, R)
            hs.append(H)
            maps.append((sc - np.median(sc)) / (sc.std() + 1e-6))
    if len(hs) < 20:
        return (0.0, 0.0)
    hs, maps = np.array(hs), np.array(maps)
    vs = np.arange(-0.4, 0.401, 0.01)
    best, lean = -1e9, (0.0, 0.0)
    k = np.arange(len(hs))
    for vx in vs:
        ix = np.clip(np.round(hs * vx / RES).astype(int) + R, 0, 2 * R)
        for vy in vs:
            iy = np.clip(np.round(-hs * vy / RES).astype(int) + R, 0, 2 * R)
            t = maps[k, iy, ix].sum()
            if t > best:
                best, lean = t, (float(vx), float(vy))
    return lean


def photo_uv(x, y):
    return np.column_stack([(x - X0) / (X1 - X0), (Y1 - y) / (Y1 - Y0)])


ATLAS = 4096                        # roof texture size, px (phones take 4096)


KIT_COLS = ["#dfe1de", "#d3d6d4", "#e8e6df", "#c9ccca", "#b9bdbd"]


def roof_kit(m, a, mask, T, U, V, u0, v0, z):
    """Air-conditioning units, vents and water tanks on a flat roof, read
    off its patch of the 20 cm photo (a, mask: texels along U, V; T texels
    per metre): small bright compact blobs that stand out from the roof
    around them. Each becomes a box of metal on the roof. Returns how many."""
    if mask.sum() < 25 * T * T:                 # roofs under ~25 m2: none
        return 0
    L = a @ np.array([0.3, 0.59, 0.11], np.float32)
    inner = ndimage.binary_erosion(mask, iterations=max(2, int(0.7 * T)))
    w = inner.astype(np.float32)
    r = 2.0 * T
    local = ndimage.gaussian_filter(L * w, r) / (ndimage.gaussian_filter(w, r) + 1e-4)
    spread = np.sqrt(np.maximum(ndimage.gaussian_filter((L - local) ** 2 * w, 3 * T) /
                                (ndimage.gaussian_filter(w, 3 * T) + 1e-4), 1.0))
    bright = inner & (L - local > np.maximum(26.0, 1.6 * spread)) & (L > 135)
    bright = ndimage.binary_opening(bright)
    lab, nb = ndimage.label(bright)
    if nb == 0:
        return 0
    cap = 2 + int(mask.sum() / (T * T) / 14)
    found = []
    for j, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        cells = lab[sl] == j
        n_ = cells.sum()
        area = n_ / (T * T)
        hgt, wid = (sl[0].stop - sl[0].start) / T, (sl[1].stop - sl[1].start) / T
        if area < 0.35 or area > 9 or n_ < 0.55 * cells.size or max(hgt, wid) > 4.5 * max(min(hgt, wid), 0.3):
            continue
        found.append((float((L[sl][cells] - local[sl][cells]).mean()), sl, area))
    found.sort(key=lambda t: -t[0])
    rng = np.random.default_rng(int(abs(u0 * 7 + v0 * 13)) % (2 ** 31))
    out = 0
    for _, sl, area in found[:cap]:
        i0, i1 = sl[1].start / T, sl[1].stop / T
        j0, j1 = sl[0].start / T, sl[0].stop / T
        # At least 60 cm a side, a little smaller than the blob (its edge
        # is half shadow, half photo blur).
        cu, cv = (i0 + i1) / 2, (j0 + j1) / 2
        hu, hv = max(0.3, (i1 - i0) / 2 - 0.05), max(0.3, (j1 - j0) / 2 - 0.05)
        corners = [(cu - hu, cv - hv), (cu + hu, cv - hv), (cu + hu, cv + hv), (cu - hu, cv + hv)]
        q = orient(Polygon([((u0 + a_) * U[0] + (v0 + b_) * V[0], (u0 + a_) * U[1] + (v0 + b_) * V[1])
                            for a_, b_ in corners]), 1.0)
        h = 0.7 if area < 1.5 else (1.0 if area < 4 else 1.4)
        col = np.array(hexrgb(KIT_COLS[int(rng.integers(len(KIT_COLS)))]), np.float32)
        add_walls(m, q, z - 0.05, z + h, z, 10, col * 0.93, PLAIN)
        add_roof(m, q, z + h, col, False)
        if area > 1.0:
            # A darker fan grille on top of the bigger units.
            g = q.buffer(-min(hu, hv) * 0.35, join_style=2)
            if isinstance(g, Polygon) and not g.is_empty:
                add_roof(m, orient(g, 1.0), z + h + 0.02, col * 0.55, False)
        out += 1
    return out


def roof_atlas(hi, photo, kit=None, stats=None):
    """The real roofs' own texture: each roof's patch of the 20 cm photo
    (shifted back by the photo's lean, tidied by clean_roof_patch), turned
    square to its outline's own rectangle (Monaco's blocks run on the
    diagonal; a north-up box wastes half the space) and packed side by side
    into one 4096 px image at as close to the photo's resolution as fits,
    instead of the 37 cm ground map the roofs used to sample. Rewrites the
    roof mesh's UVs (plan metres until now) to point into it.
    TEXCOORD_1 gets each roof's own plan metres along its rectangle, so
    the viewer lines paving and membrane seams up with the building.
    kit: a Mesh for the roof kit read off the photo (roof_kit).
    Returns (jpeg bytes, texels per metre)."""
    n = hi.size[0]
    ppm = n / (X1 - X0)
    G = 0.6                                  # gutter round every patch, m
    rects = []
    for pg, (sx, sy), _z in photo["polys"]:
        q = pg.buffer(0.6, join_style=2)
        c = np.asarray(q.minimum_rotated_rectangle.exterior.coords)[:3]
        e0, e1 = c[1] - c[0], c[2] - c[1]
        e = e0 if np.hypot(*e0) >= np.hypot(*e1) else e1
        U = e / max(np.hypot(*e), 1e-9)
        V = np.array([-U[1], U[0]])
        P = np.asarray(q.exterior.coords)[:, :2]
        pu, pv = P @ U, P @ V
        rects.append((q, U, V, pu.min() - G, pv.min() - G, pu.max() - pu.min() + 2 * G,
                      pv.max() - pv.min() + 2 * G, sx, sy))
    area = sum(r[5] * r[6] for r in rects)
    # As fine as fits, up to 1.6x the photo's own 20 cm (a sharp resample
    # beats the GPU's bilinear stretch), shrinking until the shelves fit.
    T = min(1.6 * ppm, math.sqrt(0.92 * ATLAS * ATLAS / max(area, 1)))
    order = sorted(range(len(rects)), key=lambda k: -rects[k][6])
    while True:
        place, x, y, shelf = {}, 0, 0, 0
        for k in order:
            w, h = int(math.ceil(rects[k][5] * T)), int(math.ceil(rects[k][6] * T))
            if x + w > ATLAS:
                x, y, shelf = 0, y + shelf, 0
            if y + h > ATLAS:
                break
            place[k] = (x, y, w, h)
            x += w
            shelf = max(shelf, h)
        if len(place) == len(rects):
            break
        T *= 0.97
    atlas = Image.new("RGB", (ATLAS, ATLAS), (128, 128, 128))
    debug = []
    grade = np.array([1.03, 1.0, 0.96], np.float32)
    for k, (q, U, V, u0, v0, _, _, sx, sy) in enumerate(rects):
        x, y, w, h = place[k]
        # Patch texel (i, j) -> plan point u0 + i/T along U, v0 + j/T along V
        # -> photo pixel, shifted by the lean.
        cx = (u0 * U[0] + v0 * V[0] + sx - X0) * ppm
        cy = (Y1 - (u0 * U[1] + v0 * V[1] + sy)) * ppm
        coef = (U[0] * ppm / T, V[0] * ppm / T, cx, -U[1] * ppm / T, -V[1] * ppm / T, cy)
        patch = hi.transform((w, h), Image.AFFINE, coef, resample=Image.BICUBIC)
        pm = Image.new("L", (w, h), 0)
        dpm = ImageDraw.Draw(pm)
        to_px = lambda pts: [((X * U[0] + Y * U[1] - u0) * T, (X * V[0] + Y * V[1] - v0) * T) for X, Y in pts]
        dpm.polygon(to_px(q.exterior.coords), fill=255)
        for hole in q.interiors:
            dpm.polygon(to_px(hole.coords), fill=0)
        nk = 0
        if kit is not None and not photo["tiled"][k]:
            nk = roof_kit(kit, np.asarray(patch).astype(np.float32), np.asarray(pm) > 0, T, U, V, u0, v0,
                          photo["polys"][k][2])
            if stats is not None:
                stats["roof_kit"] = stats.get("roof_kit", 0) + nk
        ph = np.asarray(clean_roof_patch(patch, pm, T)).astype(np.float32)
        if q.area > 150:
            c_ = q.centroid
            lo_, la_ = to_lonlat(np.array([c_.x]), np.array([c_.y]))
            debug.append({"lon": round(float(lo_[0]), 6), "lat": round(float(la_[0]), 6), "z": round(photo["polys"][k][2], 1),
                          "area": round(q.area), "tiled": photo["tiled"][k], "kit": nk,
                          "rgb": [round(float(v)) for v in ph[np.asarray(pm) > 0].mean(0)]})
        # The same grade as the roofs in the ground map: a little crisper
        # and warmer than the raw photo.
        gray = ph.mean(2, keepdims=True)
        ph = gray + (ph - gray) * 1.12
        ph = (ph - 118) * 1.1 + 126
        im = Image.fromarray(np.clip(ph * grade, 0, 255).astype(np.uint8))
        atlas.paste(im.filter(ImageFilter.UnsharpMask(radius=1.4, percent=70, threshold=2)), (x, y))
    mesh = photo["mesh"]
    for mi, k in photo["uvmap"]:
        _, U, V, u0, v0, _, _, _, _ = rects[k]
        x, y, _, _ = place[k]
        d = mesh.uv[mi].astype(np.float64)
        wx, wy = d[:, 0] + X0, Y1 - d[:, 1]
        lu, lv = wx * U[0] + wy * U[1] - u0, wx * V[0] + wy * V[1] - v0
        mesh.uv[mi] = np.column_stack([(x + lu * T) / ATLAS, (y + lv * T) / ATLAS]).astype(np.float32)
        mesh.uv2[mi] = np.column_stack([lu, lv]).astype(np.float32)
    b = io.BytesIO()
    atlas.save(b, "JPEG", quality=90)
    atlas.save(RAW / "roof-atlas-preview.jpg", quality=80)
    (RAW / "roof-debug.json").write_text(json.dumps(debug))
    return b.getvalue(), round(T, 2)


def tidy_outline(poly, r=0.45):
    """Footprint without the small jogs, notches and spikes a traced
    outline (or a 1 m raster region) carries: an opening and a closing of
    r metres with square joins, then a light simplify. Keeps the polygon
    when tidying would lose more than a few percent of it."""
    try:
        q = poly.buffer(-r, join_style=2).buffer(r, join_style=2)
        q = q.buffer(r, join_style=2).buffer(-r, join_style=2)
        q = q.simplify(0.25)
    except Exception:
        return poly
    parts = [g for g in to_polys(q) if g.area > 1]
    if not parts:
        return poly
    q = max(parts, key=lambda g: g.area)
    if not q.is_valid or abs(q.area - poly.area) > 0.06 * poly.area:
        return poly
    return orient(q, 1.0)


def skeleton_roof(poly, z, slope, t_max, dt=0.35):
    """A roof whose slopes all rise at `slope` (m per m) from every eave,
    up to t_max metres in from the eaves and flat above: hipped roofs
    (t_max large), mansards (slope steep, t_max = the mansard's depth).
    The contour lines are the outline offset inwards with mitred corners
    (the straight skeleton's wavefront), so hips, valleys and ridges are
    straight lines and the eave is crisp, not a mesh of random facets.
    Returns (xy vertices, z, triangles) or None."""
    rings, zs, holes = [], [], []
    levels = np.arange(0.0, t_max + 1e-6, dt)
    if levels[-1] < t_max - 0.05:
        levels = np.append(levels, t_max)
    for t in levels:
        g = poly if t == 0 else poly.buffer(-t, join_style=2, mitre_limit=20.0)
        ps = [orient(q.simplify(0.04), 1.0) for q in to_polys(g) if q.area > 0.3]
        if not ps:
            break
        for q in ps:
            for ring in [q.exterior] + list(q.interiors):
                pts = np.asarray(ring.coords)[:-1, :2]
                if len(pts) >= 3:
                    rings.append(pts)
                    zs.append(z + t * slope)
    for hole in poly.interiors:
        hp = Polygon(hole).representative_point()
        holes.append([hp.x, hp.y])
    verts, segs, vz = [], [], []
    for pts, zz in zip(rings, zs):
        b = len(verts)
        n = len(pts)
        verts.extend(pts.tolist())
        vz.extend([zz] * n)
        segs.extend([[b + i, b + (i + 1) % n] for i in range(n)])
    data = {"vertices": np.array(verts), "segments": np.array(segs)}
    if holes:
        data["holes"] = np.array(holes)
    try:
        t = triangle.triangulate(data, "p")
    except Exception:
        return None
    if "triangles" not in t or not len(t["triangles"]):
        return None
    v = t["vertices"]
    vz = np.asarray(vz, float)
    if len(v) > len(vz):
        # Points triangle added where two contours touched: their height
        # from the distance to the eaves.
        extra = v[len(vz):]
        d = shapely.distance(poly.boundary, shapely.points(extra[:, 0], extra[:, 1]))
        vz = np.concatenate([vz, z + np.minimum(d, t_max) * slope])
    return v, vz, t["triangles"]


def add_roof(m, poly, z, col, tiled, cap=None, photo=None):
    """photo: a dict {"mesh", "polys"}; the roof goes into that textured
    mesh, mapped onto the aerial photo painted into the ground map."""
    if tiled:
        slope = math.tan(math.radians(28))
        t = skeleton_roof(poly, z, slope, (cap if cap else 99) / slope, dt=0.4)
        if t is None:
            return
        v, zz, tri = t
    else:
        t = triangulate(poly)
        if t is None:
            return
        v, tri = t
        zz = np.full(len(v), z)
    P = enu_fast(v[:, 0], v[:, 1], zz)
    if photo is not None:
        # UVs hold the plan position (metres east of X0, south of Y1) for
        # now; roof_atlas() turns them into this roof's place in its texture.
        uv = np.column_stack([v[:, 0] - X0, Y1 - v[:, 1]])
        photo["mesh"].tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], [255, 255, 255], ROOF_FLAT,
                          uv=np.stack([uv[tri[:, 0]], uv[tri[:, 1]], uv[tri[:, 2]]], 1), style=PHOTO_ROOF)
        photo["uvmap"].append((len(photo["mesh"].uv) - 1, len(photo["polys"])))
        photo["polys"].append((poly, photo["shift"], z))
        photo["tiled"].append(bool(tiled))
        return
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


# The Casino's front on the square: two corner towers with tall copper caps
# and a raised centre with its clock, as on Garnier's façade.
CASINO_FRONT = "BATIMENT0000000074070299"
CASINO_SQUARE = (1056553.0, 6303373.0)      # the Casino fountain on the square, L93


def casino_front(m, poly, base, H, fh, col, kind, style, rcol, mntR=None):
    import shapely
    sq = shapely.Point(*CASINO_SQUARE)
    rect = np.asarray(poly.minimum_rotated_rectangle.exterior.coords)[:4]
    sides = [(rect[i], rect[(i + 1) % 4]) for i in range(4)]
    a, b = min(sides, key=lambda e: shapely.Point((e[0] + e[1]) / 2).distance(sq))
    u = (b - a) / np.linalg.norm(b - a)
    n = np.array([-u[1], u[0]])
    mid = (a + b) / 2
    if np.dot(n, np.asarray(CASINO_SQUARE) - mid) > 0:
        n = -n                                  # n points into the building
    L = float(np.linalg.norm(b - a))
    zt = base + H
    copper = np.asarray(rcol)

    def block(c, w, d, z0, z1):
        pts = [c + u * w / 2 + n * d / 2, c - u * w / 2 + n * d / 2, c - u * w / 2 - n * d / 2, c + u * w / 2 - n * d / 2]
        return orient(Polygon(pts), 1.0)
    for t_ in (0.12, 0.88):
        c = a + u * L * t_ + n * 3.6           # flush with the façade
        tp = block(c, 7.5, 7.5, zt - 2, zt + 7)
        add_walls(m, tp, zt - 2, zt + 7, base, fh, col, kind, style=style)
        add_walls(m, tp.buffer(0.35, join_style=2), zt + 6.6, zt + 7.3, zt, 10, np.asarray(col) * 1.05, PLAIN)
        add_cap(m, tp, zt + 7.3, 7.5, copper, ROOF_TILE)
        add_cap(m, block(c, 1.2, 1.2, 0, 0), zt + 14.4, 2.2, hexrgb("#d9b75e"), PLAIN)   # gilded finial
    c = mid + n * 2.9
    cp = block(c, 14, 6, zt - 2, zt + 5)
    add_walls(m, cp, zt - 2, zt + 5, base, fh, col, kind, style=style)
    add_cap(m, cp, zt + 5, 3.5, copper, ROOF_TILE)
    clock = block(mid - n * 0.05, 2.6, 0.3, 0, 0)
    add_walls(m, clock, zt + 1.2, zt + 3.8, zt, 10, hexrgb("#f4efe2"), PLAIN)

    # The entrance on the square, as built: a flight of broad steps, three
    # tall arched doors, the glass-and-iron marquise over them, and a
    # bronze candelabrum either side.
    q = poly.exterior.interpolate(poly.exterior.project(shapely.Point(*mid)))
    f0 = np.array([q.x, q.y])                    # on the façade line
    stone = np.array(hexrgb("#e9e0cc"))
    # The square is well above the building's lowest corner (it falls away
    # to the sea behind): the entrance stands at the square's level.
    if mntR is not None:
        zs = mntR.sample(np.array([f0[0] - n[0] * 8]), np.array([f0[1] - n[1] * 8]))
        if np.isfinite(zs[0]):
            base = float(zs[0]) - 0.6
    for k in range(4):
        d0 = 0.45 * (4 - k)
        c = f0 - n * (d0 / 2 + 0.02)
        add_walls(m, block(c, 22 - k * 0.8, d0, 0, 0), base - 0.3, base + 0.17 * (k + 1), base, 10, stone * (0.94 + 0.02 * k), PLAIN)
        add_roof(m, block(c, 22 - k * 0.8, d0, 0, 0), base + 0.17 * (k + 1), stone, False)
    zf = base + 0.68
    door = np.array(hexrgb("#2a2622"))
    trim = np.minimum(np.asarray(col) * 1.06 + 6, 250)
    for j in (-1, 0, 1):
        c = f0 + u * j * 4.0 - n * 0.06
        w, h = 2.6, 5.2
        # Rectangle below the springing line, then a half-round fan.
        A = [c + u * w / 2, c - u * w / 2]
        P = enu_fast(np.array([A[0][0], A[1][0], A[1][0], A[0][0]]), np.array([A[0][1], A[1][1], A[1][1], A[0][1]]),
                     np.array([zf, zf, zf + h - w / 2, zf + h - w / 2]))
        m.tri(P[[0, 0]], P[[1, 2]], P[[2, 3]], door, PLAIN)
        m.tri(P[[0, 0]], P[[2, 1]], P[[3, 2]], door, PLAIN)
        ang = np.linspace(0, math.pi, 9)
        ring = [c + u * (w / 2) * math.cos(a_) for a_ in ang]
        zz = zf + h - w / 2 + (w / 2) * np.sin(ang)
        R_ = enu_fast(np.array([r[0] for r in ring]), np.array([r[1] for r in ring]), zz)
        C_ = enu_fast(np.array([c[0]]), np.array([c[1]]), np.array([zf + h - w / 2]))
        for i in range(8):
            m.tri(C_, R_[[i]], R_[[i + 1]], door, PLAIN)
            m.tri(C_, R_[[i + 1]], R_[[i]], door, PLAIN)
        # Pilasters between the doors.
        for s_ in (-1, 1):
            add_walls(m, block(c + u * s_ * 2.0 - n * 0.15, 0.6, 0.4, 0, 0), base, zf + h + 0.6, base, 10, trim, PLAIN)
    # Marquise: a thin glazed canopy on an iron frame, cantilevered.
    mq = block(f0 - n * 2.4, 15.0, 4.8, 0, 0)
    zm = zf + 5.9
    add_walls(m, mq, zm, zm + 0.35, base, 10, hexrgb("#2e3936"), PLAIN)
    add_roof(m, mq, zm + 0.35, np.array(hexrgb("#a9bcbc")), False)
    add_roof(m, orient(mq, -1.0), zm, np.array(hexrgb("#3a4643")), False)
    for s_ in (-1, 1):
        lp = f0 - n * 5.6 + u * s_ * 11.5
        _box(m, lp[0], lp[1], u, 0.7, 0.7, base, base + 1.2, stone)
        _box(m, lp[0], lp[1], u, 0.22, 0.22, base + 1.2, base + 4.6, hexrgb("#4a3f2c"), style=BRONZE)
        for t_ in (-1, 0, 1):
            pp = lp + u * t_ * 0.55
            _ball(m, pp[0], pp[1], base + 4.9 + (0.25 if t_ == 0 else 0), 0.24, np.array(hexrgb("#f4ecd6")), 3, 8, 5)


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
        # Real wall colours of the buildings facing the circuit, sampled from
        # Mapillary street photos by facade_colours.py.
        f = Path(__file__).resolve().parent / "facade_colours.json"
        self.photo_colours = ({k: v["colour"] for k, v in json.load(open(f))["buildings"].items()}
                              if f.exists() else {})
        self.used_photo_colours = 0
        # What the street photos show of each building facing the circuit:
        # its façade type (and a colour where the sampling went wrong).
        f = Path(__file__).resolve().parent / "facade_survey.json"
        names = {"shutter": S_SHUTTER, "belle": S_BELLE, "arch": S_ARCH, "villa": S_VILLA, "loggia": S_LOGGIA,
                 "ribbon": S_RIBBON, "glass": S_GLASS, "modern": S_MODERN, "deco": S_DECO}
        # Also storeys, window spacing and balconies counted in the photos.
        self.survey = {k: {"style": names[v["style"]], "colour": v.get("colour"), "levels": v.get("levels"),
                           "bay": v.get("bay"), "balconies": v.get("balconies"), "depth": v.get("depth")}
                       for k, v in (json.load(open(f))["buildings"].items() if f.exists() else [])}
        self.used_survey = 0

    def get(self, poly, props):
        out = {"levels": None, "year": None, "name": None, "style": None, "colour": None,
               "bay": None, "balconies": None, "depth": None}
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
        if sv0 := self.survey.get(props.get("cleabs")):
            out["levels"] = sv0["levels"] or out["levels"]
            out["photo_levels"] = sv0["levels"]
        if not out["year"] and (props.get("date_d_apparition") or "")[:4].isdigit():
            out["year"] = int(props["date_d_apparition"][:4])
        pc = self.photo_colours.get(props.get("cleabs"))
        sv = self.survey.get(props.get("cleabs"))
        if sv:
            out["style"] = sv["style"]
            pc = sv["colour"] or pc
            out.update({k: sv[k] for k in ("bay", "balconies", "depth") if sv[k]})
            self.used_survey += 1
        if pc:
            out["colour"] = pc
        for pt, name, st, colour in self.marks:
            if poly.buffer(3).contains(pt):
                out.update(style=st, colour=colour, name=name)
        if pc and out["colour"] == pc:
            self.used_photo_colours += 1
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
    """Steep roof skirt rising slope_h over the first `inset` metres, then
    flat, with straight hips at the corners and a crisp break line."""
    t = skeleton_roof(poly, z, slope_h / inset, inset, dt=inset / 3)
    if t is None:
        return
    v, zz, tri = t
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


def tribune_steps(m, poly, mntR, H):
    """Open stepped seating on a stand's outline, low on the circuit side
    and rising away from it."""
    rect = np.asarray(poly.minimum_rotated_rectangle.exterior.coords)[:4]
    e0, e1 = rect[1] - rect[0], rect[2] - rect[1]
    if np.linalg.norm(e0) < np.linalg.norm(e1):
        rect = np.roll(rect, -1, 0)
        e0, e1 = rect[1] - rect[0], rect[2] - rect[1]
    depth = float(np.linalg.norm(e1))
    ring = poly.exterior
    s = np.linspace(0, ring.length, 24)
    pts_ = shapely.line_interpolate_point(ring, s)
    z0 = float(np.nanmin(mntR.sample(shapely.get_x(pts_), shapely.get_y(pts_))))
    if not np.isfinite(z0):
        return
    # Low edge on the circuit side, rising away from it.
    a, b = rect[0], rect[1]
    out = e1 / depth
    if TRACK is not None and track_dist(*(rect[0] + e0 / 2))[0] > track_dist(*(rect[0] + e0 / 2 + e1))[0]:
        a, b, out = rect[3], rect[2], -out
    n = 5
    stone = np.array(hexrgb("#e4ddcf"))
    for k in range(n):
        d0, d1 = depth * k / n, depth * (k + 1) / n
        pts = [a + out * d0, b + out * d0, b + out * d1, a + out * d1]
        q = orient(Polygon(pts), 1.0).intersection(poly.buffer(0.3))
        if q.is_empty or q.geom_type != "Polygon":
            continue
        z1 = z0 + H * (k + 1) / n
        add_walls(m, orient(q, 1.0), z0 - 0.2, z1, z0, 100, stone * (0.92 if k % 2 else 1.0), PLAIN)
        add_roof(m, orient(q, 1.0), z1, stone, False)


AWNINGS = ["#2f5d4a", "#7a2630", "#e8dfc8", "#24344f", "#b5532f", "#3c3c3c", "#d8c27a"]


def facade_depth(m, poly, base, H, fh, style, col, rnd, balconies=None, depth=None):
    """Real relief on the façades that face the circuit, where the lap
    camera sees them: continuous balconies with their railings on the
    1960s-80s blocks (Boulevard Albert Ier, the port), small stone
    balconies on the older ones, and shop awnings over the pavement."""
    if TRACK is None or H < 7:
        return 0
    if TUNNEL is not None and poly.intersects(TUNNEL["bld"]):
        return 0
    keep = TRACK["line"].buffer(TRACK_HALF + 1.2)
    ring = np.asarray(orient(poly, 1.0).exterior.coords)[:, :2]
    modern = style in (S_LOGGIA, S_RIBBON, S_MODERN)
    old = style in (S_SHUTTER, S_BELLE, S_DECO, S_VILLA)
    if balconies:
        # What the street photos show wins over the guess from the style.
        modern, old = balconies == "continuous", balconies == "small"
    if not (modern or old) and balconies != "none":
        return 0
    n_ = 0
    pale = np.minimum(np.asarray(col) * 1.06 + 10, 252)
    if style == S_BELLE:
        pale = np.array(hexrgb("#efe8da"))      # Belle Époque: white stone balconies
    for a, b in zip(ring[:-1], ring[1:]):
        L = float(np.linalg.norm(b - a))
        if L < 6:
            continue
        u = (b - a) / L
        out = np.array([u[1], -u[0]])          # CCW ring: right-hand side is outside
        mid = (a + b) / 2
        d_mid = float(track_dist(*(mid + out * 2.0))[0])
        if d_mid > 40 or float(track_dist(*(mid + out * 8))[0]) > d_mid + 1:
            continue                            # not facing the circuit
        a2, b2 = a + u * 0.6, b - u * 0.6

        def slab(z0, z1, dep, colr, w0=a2, w1=b2):
            q = Polygon([w0, w1, w1 + out * dep, w0 + out * dep])
            q = q.difference(keep)
            for qq in to_polys(q):
                if qq.area < 0.8:
                    continue
                qq = orient(qq, 1.0)
                add_walls(m, qq, z0, z1, z0, 100, colr, PLAIN)
                add_roof(m, qq, z1, colr, False)
                add_roof(m, orient(qq, -1.0), z0, colr * 0.8, False)
        floors = int(H / fh)
        if modern:
            rail = np.array(hexrgb(rnd.choice(["#f4f2ec", "#e9e6de", "#5f6a6e", "#d9d4c8"])))
            if style == S_BELLE:
                rail = pale * 0.98                  # a white balustrade
            dep = depth or rnd.uniform(1.0, 1.5)
            for k in range(1, floors):
                z = base + k * fh
                if z > base + H - 1.5:
                    break
                slab(z - 0.2, z, dep, pale)
                # The railing: a solid parapet or tinted glass along the front.
                q = Polygon([a2 + out * (dep - 0.08), b2 + out * (dep - 0.08), b2 + out * dep, a2 + out * dep])
                for qq in to_polys(q.difference(keep)):
                    if qq.area > 0.1:
                        add_walls(m, orient(qq, 1.0), z, z + 1.0, z, 100, rail, PLAIN)
                        add_roof(m, orient(qq, 1.0), z + 1.0, rail, False)
                n_ += 1
        elif old:
            # A balcony under every other bay on the first floors.
            bay = BAY_NOW or STYLE_BAY.get(style, 3.2)
            nb = max(1, int(round(L / bay)))
            for k in range(1, floors if balconies == "small" else min(floors, 6)):
                z = base + k * fh
                if z > base + H - 2:
                    break
                for j in range(nb):
                    if (j + k) % 2:
                        continue
                    c = a + u * (j + 0.5) * L / nb
                    w0, w1 = c - u * 0.8, c + u * 0.8
                    slab(z - 0.18, z, 0.6, pale, w0, w1)
                    # A low stone balustrade (a solid iron sheet would read
                    # as a black box; real ironwork is see-through).
                    q = Polygon([w0 + out * 0.5, w1 + out * 0.5, w1 + out * 0.6, w0 + out * 0.6])
                    for qq in to_polys(q.difference(keep)):
                        if qq.area > 0.03:
                            add_walls(m, orient(qq, 1.0), z, z + 0.45, z, 100, pale * 0.96, PLAIN)
                            add_roof(m, orient(qq, 1.0), z + 0.45, pale, False)
                n_ += 1
        # Shop awnings over the pavement at street level, only in front of
        # real shops.
        spans = shop_spans(a, b)
        if d_mid < 28 and spans:
            seg = max(1, int(L / 6.5))
            for j in range(seg):
                if rnd.random() < 0.3 or not any(s0 <= (j + 0.5) / seg <= s1 for s0, s1 in spans):
                    continue
                w0 = a + u * (j * L / seg + 0.5)
                w1 = a + u * ((j + 1) * L / seg - 0.5)
                ac = np.array(hexrgb(rnd.choice(AWNINGS)))
                P0, P1 = w0, w1
                P2, P3 = w1 + out * 1.7, w0 + out * 1.7
                q = Polygon([P0, P1, P2, P3]).difference(keep)
                if q.is_empty or q.area < 3:
                    continue
                pts = np.asarray(orient(q.convex_hull, 1.0).exterior.coords)[:-1]
                dist = (pts - a) @ out
                zz = base + 3.1 - 0.35 * dist
                P = enu_fast(pts[:, 0], pts[:, 1], zz)
                t = np.arange(1, len(P) - 1)
                m.tri(P[np.zeros_like(t)], P[t], P[t + 1], ac, PLAIN)
                m.tri(P[np.zeros_like(t)], P[t + 1], P[t], ac * 0.75, PLAIN)
                # Valance: the hanging front flap.
                fr = Polygon([P3 - u * 0.0, P2, P2 + out * 0.04, P3 + out * 0.04])
                if not fr.intersects(keep):             # (the flap is never cut: whole or not at all)
                    add_walls(m, orient(fr, 1.0), base + 3.1 - 0.35 * 1.7 - 0.3, base + 3.1 - 0.35 * 1.7, base, 100, ac * 0.9, PLAIN)
    return n_


# Église Sainte-Dévote, at the foot of its ravine just above the first
# corner. BD TOPO has it 2.4 m tall (the scan under the bridge); the LiDAR
# surface shows a nave about 9 m to the ridge, gable on to the square, and a
# bell tower about 16 m tall near the east corner of the front. The outline's
# main body (the rest, up the slope, is the presbytery): four corners, L93.
CHURCH_SD = "BATIMENT0000000074074425"
CHURCH_SD_BODY = [(1056048.4, 6303135.9), (1056040.7, 6303123.1), (1056014.4, 6303141.3), (1056024.1, 6303152.7)]
CHURCH_SD_TOWER = (1056043.2, 6303134.2)


def church_sainte_devote(m, mntR):
    global BAY_NOW
    body = orient(Polygon(CHURCH_SD_BODY), 1.0)
    F0, F1, Bk1, Bk0 = (np.array(p_) for p_ in CHURCH_SD_BODY)
    fm, bm = (F0 + F1) / 2, (Bk0 + Bk1) / 2
    L = float(np.linalg.norm(bm - fm))
    u = (bm - fm) / L                       # from the front to the back
    v = np.array([-u[1], u[0]])             # across
    hw = float(np.linalg.norm(F0 - F1)) / 2
    if np.dot(F0 - fm, v) < 0:
        v = -v
    base = float(np.nanmin(mntR.sample(np.array([F0[0], F1[0], fm[0]]), np.array([F0[1], F1[1], fm[1]]))))
    cream, trim = np.array(hexrgb("#f0e4c9")), np.array(hexrgb("#e1c393"))
    tile = np.array(hexrgb("#b8583b"))
    eave, ridge = base + 6.8, base + 9.9

    def P(sv, tv, zv):
        q = fm[None] + np.outer(np.atleast_1d(sv), u) + np.outer(np.atleast_1d(tv), v)
        return enu_fast(q[:, 0], q[:, 1], np.atleast_1d(np.asarray(zv, float)) * np.ones(len(q)))

    sky = P(L / 2, 0, ridge + 40)
    # Walls: one tall storey of round-arched windows.
    BAY_NOW = 4.4
    add_walls(m, body, base - 1.0, eave, base, eave - base, cream, WALL_OLD, style=S_ARCH)
    # Cornice under the eaves.
    cor = body.buffer(0.35, join_style=2)
    add_walls(m, cor, eave - 0.45, eave, eave, 10, trim, PLAIN)
    # Gable roof along the nave, 40 cm over the walls all round.
    o = 0.55
    for side in (-1, 1):
        A = P([-o, L + o], [side * (hw + o)] * 2, eave - 0.2)
        R = P([-o, L + o], [0, 0], ridge)
        tri_facing(m, A[[0, 0]], A[[1, 1]], R[[1, 0]], np.repeat(sky, 2, 0), tile, ROOF_TILE)
        tri_facing(m, A[[0]], R[[1]], R[[0]], sky, tile, ROOF_TILE)
        # Underside of the overhang.
        A2 = P([-o, L + o], [side * hw] * 2, eave - 0.05)
        dn = P(L / 2, side * hw, eave - 30)
        tri_facing(m, A[[0, 0]], A[[1, 1]], A2[[1, 0]], np.repeat(dn, 2, 0), trim * 0.8, PLAIN)
    # Gable ends.
    for sv, outw in ((0.0, -1), (L, 1)):
        G = P([sv] * 3, [-hw, hw, 0], [eave, eave, ridge - 0.1])
        tri_facing(m, G[[0]], G[[1]], G[[2]], P(sv + outw * 20, 0, eave), cream, PLAIN)
    # The front: a frontispiece with pilasters and a pediment above the
    # roof line, the arched portal in the middle.
    wf = hw * 0.62
    BAY_NOW = 2 * wf
    front = orient(Polygon([tuple(fm - u * 0.9 - v * wf), tuple(fm - u * 0.9 + v * wf),
                            tuple(fm + u * 0.2 + v * wf), tuple(fm + u * 0.2 - v * wf)]), 1.0)
    ztop = base + 10.2
    add_walls(m, front, base - 1.0, ztop, base, ztop - base, cream, WALL_OLD, style=S_ARCH)
    for tv in (-wf, wf):                       # pilasters at the corners
        c_ = fm - u * 0.95 + v * tv * 0.97
        _box(m, c_[0], c_[1], v, 0.7, 0.35, base - 0.5, ztop, trim, PLAIN)
    corn = orient(front.buffer(0.3, join_style=2), 1.0)
    add_walls(m, corn, ztop - 0.5, ztop, ztop, 10, trim, PLAIN)
    add_roof(m, corn, ztop, trim, False)
    # Pediment.
    pz = ztop + 2.4
    for so in (-1.2, 0.2):
        G = P([so] * 3, [-wf - 0.3, wf + 0.3, 0], [ztop, ztop, pz])
        tri_facing(m, G[[0]], G[[1]], G[[2]], P(so - 20 if so < 0 else so + 20, 0, ztop), cream if so < 0 else cream * 0.95, PLAIN)
    for side in (-1, 1):
        A = P([-1.2, 0.2], [side * (wf + 0.3)] * 2, ztop)
        R = P([-1.2, 0.2], [0, 0], pz)
        tri_facing(m, A[[0, 0]], A[[1, 1]], R[[1, 0]], np.repeat(P(-0.5, 0, pz + 30), 2, 0), tile, ROOF_TILE)
    # Cross on the apex.
    c_ = fm - u * 0.5
    _box(m, c_[0], c_[1], v, 0.14, 0.14, pz, pz + 1.3, trim, PLAIN)
    _box(m, c_[0], c_[1] + 0.0, v, 0.7, 0.14, pz + 0.75, pz + 0.92, trim, PLAIN)
    # Bell tower: square, three stages of arches, a cornice and a tiled cap.
    tx, ty = CHURCH_SD_TOWER
    tw = 3.8
    tp = orient(Polygon([tuple(np.array([tx, ty]) + a_ * u * tw / 2 + b_ * v * tw / 2)
                         for a_, b_ in ((-1, -1), (-1, 1), (1, 1), (1, -1))]), 1.0)
    BAY_NOW = tw
    th = base + 14.0
    add_walls(m, tp, base, th, base, (th - base) / 3, cream, WALL_OLD, style=S_ARCH)
    tc = orient(tp.buffer(0.35, join_style=2), 1.0)
    add_walls(m, tc, th - 0.4, th + 0.1, th, 10, trim, PLAIN)
    add_roof(m, tc, th + 0.1, trim, False)
    add_cap(m, orient(tp.buffer(-0.1, join_style=2), 1.0), th + 0.1, 2.6, tile, ROOF_TILE)
    BAY_NOW = None
    return body.union(tp)


def buildings(m, feats, mns, mnt, ortho, stats, facades, photo=None):
    global BAY_NOW, GROUND_R, SHOP_NOW
    mntR = Raster(mnt)
    GROUND_R = mntR
    # Shops whose point lies inside some outline belong to that building only.
    in_any = set()
    if SHOPS is not None and len(SHOPS["pts"]):
        allb = unary_union([shapely.force_2d(shape(f["geometry"])).buffer(0) for f in feats])
        in_any = set(np.nonzero(contains_xy(allb, SHOPS["pts"][:, 0], SHOPS["pts"][:, 1]))[0].tolist())
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
            poly = tidy_outline(poly)
            if TRACK is not None and poly.intersects(TRACK["keep_out"]) and not (
                    TUNNEL is not None and poly.intersects(TUNNEL["bld"])):
                # Outlines that reach onto the track (arcades, kiosks,
                # misaligned footprints) stop at its edge.
                rest = [q for q in to_polys(poly.difference(TRACK["keep_out"])) if q.area >= 10]
                if not rest:
                    continue
                poly = orient(max(rest, key=lambda q: q.area), 1.0)
            key = f["properties"].get("cleabs") or poly.wkt[:60]
            if key == CHURCH_SD:
                body = church_sainte_devote(m, mntR)
                footprint.append(body)
                stats["landmarks"] = stats.get("landmarks", 0) + 1
                rest = [q for q in to_polys(poly.difference(body.buffer(0.4, join_style=2))) if q.area >= 10]
                if not rest:
                    continue
                poly = orient(max(rest, key=lambda q: q.area), 1.0)
            rnd = rng_for(key)
            pr_ = f["properties"]
            if pr_.get("construction_legere") and float(track_dist(poly.centroid.x, poly.centroid.y)[0]) < 30:
                # Light structures by the circuit are Grand Prix temporaries
                # the 2021 LiDAR caught (the shed north of the pool).
                stats["dropped"] += 1
                continue
            if pr_.get("nature") == "Tribune":
                # A low stand (the Rainier III pool's): open stone steps at
                # its BD TOPO height, not a block of flats. The LiDAR has the
                # Grand Prix grandstand on top of it.
                tribune_steps(m, poly, mntR, float(pr_.get("hauteur") or 2.0))
                continue
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
            if TRACK is not None and poly.distance(TRACK["line"]) < 8:
                # A building down the slope below the circuit (Avenue
                # d'Ostende's sea side): its roof is a terrace at the road's
                # level at most, whatever stands on it (lamps, hedges, the
                # street itself where the outline runs under it).
                rp_ = poly.representative_point()
                zr_ = float(TRACK["z"][TRACK["kd"].query([rp_.x, rp_.y])[1]])
                if base < zr_ - 6 and np.median(hrel) + base < zr_ + 2:
                    top = min(top, zr_ - 0.3 - base)
                    hrel = np.minimum(hrel, top)
            props = f["properties"]
            # The LiDAR was flown in 2021; BD TOPO is current. A building
            # the scan does not see is either newer (Mareterra and its
            # neighbours, 2022 onwards: keep it, at its BD TOPO height, in a
            # modern style) or something that no longer stands, a shed or a
            # canopy (drop it: these were the stray blocks).
            built = float(np.mean(hrel > 2.5))
            recent = (props.get("date_creation") or "") >= "2022"
            post_scan = False
            if built < 0.35 or props.get("mareterra"):
                # (Mareterra's blocks always: the 2021 scan caught some of
                # them half-built, which made Le Renzo 2 a flat slab.)
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
                            region = unary_union([tidy_outline(q, 0.8) for q in to_polys(region)])
                            region = region.intersection(prev)     # (tidying never grows it past the level below)
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
            BAY_NOW = None if hero else info["bay"]
            style = S_ARCH if hero else pick_style(info, H, tiled, grand, post_scan, rnd)
            mt = props.get("mareterra")
            if mt is not None:
                # Mareterra: white walls and wide windows behind white
                # slab balconies, the Renzo Piano blocks included.
                style = S_MODERN
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
            if mt is not None:
                col = np.array(hexrgb(MT_WHITE)) * (0.97 + 0.03 * rnd.random())
            # Storeys: the real count where open data has it, so each
            # building gets its own number of rows of windows.
            hwall = max(2.5, float(np.percentile(hrel, 40))) if tiled else H
            fh = STYLE_FH[style]
            pl = info.get("photo_levels")
            if pl and 2.7 <= (hwall - 1.5) / pl <= 3.9:
                # Storeys counted in the street photos; the LiDAR height
                # also holds a parapet or roof plant of a metre or two.
                fh = (hwall - 1.5) / pl
                stats["real_floors"] += 1
            elif info["levels"] and 2.5 <= hwall / info["levels"] <= 5.8:
                fh = hwall / info["levels"]
                stats["real_floors"] += 1
            elif len(levels) == 1:
                fh = hwall / max(1, round(hwall / fh))
            if hero:
                # Garnier's halls: tall arcaded ground floor, a piano nobile
                # and an attic, not rows of flats.
                fh = hwall / max(2, round(hwall / 7.5))
            if mt is not None:
                fh = max(2.8, (H - 1.0) / mt["levels"])
            # Fine-tune this building's photo shift by up to a metre.
            adj = (0.0, 0.0)
            if photo is not None and photo.get("edges") is not None and poly.area > 80:
                lx, ly = photo["lean"]
                sc = outline_score(photo["edges"], poly, 2 + int(round(abs(H * lx) / RES + abs(H * ly) / RES)))
                R_ = (sc.shape[0] - 1) // 2
                px_, py_ = int(round(H * lx / RES)), int(round(-H * ly / RES))
                win = sc[R_ + py_ - 2:R_ + py_ + 3, R_ + px_ - 2:R_ + px_ + 3]
                if win.shape == (5, 5) and win.max() > win[2, 2] * 1.06:
                    iy_, ix_ = np.unravel_index(np.argmax(win), win.shape)
                    adj = ((ix_ - 2) * RES, -(iy_ - 2) * RES)
            SHOP_NOW = None if hero else shops_for(poly, SHOPS, in_any)
            if SHOP_NOW is not None:
                stats["shop_buildings"] = stats.get("shop_buildings", 0) + 1
            zb = base - 1.0
            for li, (region, hz) in enumerate(levels):
                zt = base + hz
                last = li == len(levels) - 1
                ph = None
                if photo is not None and not hero and not post_scan:
                    lx, ly = photo["lean"]
                    ph = dict(photo, shift=(hz * lx + adj[0], hz * ly + adj[1]))
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
                        # Eaves that overhang the walls by 40 cm, with a
                        # thin fascia board and a soffit underneath.
                        ov = part.buffer(0.4, join_style=2)
                        if not isinstance(ov, Polygon):
                            ov = part
                        ov = orient(ov, 1.0)
                        add_roof(m, ov, eave, rcol, True, max(1.0, base + top - eave), photo=ph)
                        if ov is not part:
                            fas = np.asarray(col) * 0.92
                            add_walls(m, ov, eave - 0.16, eave, eave, 10, fas, PLAIN)
                            for sp in to_polys(ov.difference(part)):
                                add_roof(m, orient(sp, -1.0), eave - 0.16, fas * 0.8, False)
                    elif grand and last and part.area > 120 and hz > 8:
                        sk = min(3.4, hz * 0.25)
                        add_walls(m, part, zb, zt - sk, base, fh, col, kind, style=style)
                        add_mansard(m, part, zt - sk, rcol if rk == "copper" else hexrgb("#7d8a8f"),
                                    ROOF_TILE, sk, 2.2)
                    else:
                        add_walls(m, part, zb, zt, base, fh, col, kind, style=style)
                        add_roof(m, part, zt, rcol, False, photo=ph)
                        if part.area > 40:
                            # Parapet: a low wall round the roof edge, so
                            # roofs read as terraces instead of lids.
                            pc = np.asarray(col) * 0.97
                            if style == S_BELLE:
                                # Belle Époque roofs end in a pale stone balustrade.
                                pc = np.array(hexrgb("#f1ebdf"))
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
                # Only where the photo (shifted by its lean, so a street
                # tree beside a tall building does not count) shows clear
                # green AND the LiDAR sees something standing on the roof.
                lx_, ly_ = photo["lean"] if photo is not None else (0.0, 0.0)
                cx2 = ((gx[ins] + H * lx_ - X0) / RES).astype(int)
                cy2 = ((Y1 - gy[ins] - H * ly_) / RES).astype(int)
                cell_rgb = ortho[np.clip(cy2, 0, ortho.shape[0] - 1), np.clip(cx2, 0, ortho.shape[1] - 1)].astype(float)
                exg = 2 * cell_rgb[:, 1] - cell_rgb[:, 0] - cell_rgb[:, 2]
                roof_z = float(np.percentile(hrel, 50))
                green = np.nonzero((exg > 30) & (hrel > roof_z + 0.8) & (hrel < H + 6))[0]
                if len(green) >= 10:
                    gxy = np.column_stack([gx[ins][green], gy[ins][green]])
                    picked = []
                    for k_ in rnd.permutation(len(gxy)):
                        if len(picked) >= min(10, len(green) // 8):
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
            if props.get("cleabs") == CASINO_FRONT:
                casino_front(m, poly, base, H, fh, col, kind, style, rcol, mntR)
            elif mt is not None:
                stats["mareterra_slabs"] = stats.get("mareterra_slabs", 0) + mareterra_terraces(
                    m, poly, base, H, fh, mt)
            elif not hero and not post_scan and (not tiled or info["balconies"]):
                stats["balconies"] = stats.get("balconies", 0) + facade_depth(
                    m, levels[0][0], base, hwall if tiled else levels[0][1], fh, style, col, rnd,
                    info["balconies"], info["depth"])
            SHOP_NOW = None
            footprint.append(poly)
            stats["buildings"] += 1
            stats["levels"] += len(levels)
    BAY_NOW = GROUND_R = SHOP_NOW = None
    return footprint


# -------------------------------------------------------------- Mareterra ----
# Mareterra (L'Anse du Portier), the 6 ha land extension east of the Grimaldi
# Forum, opened in December 2024. The LiDAR (2021) and the IGN photo (2022)
# show it as a building site: pits below sea level, cranes, half-built
# floors. Here it is rebuilt from what was mapped after it opened:
# OpenStreetMap (outline, buildings with their floors, the park, the pond,
# pools, paths, the Port du Petit Portier marina), BD TOPO 2024 heights where
# OpenStreetMap has none, and street photos on Mapillary (2025) for the look:
# white terraced blocks with deep slab balconies, pale stone paving, young
# pines. Nothing comes from Google's photorealistic tiles: their terms forbid
# deriving geometry or textures from them.
MT_NAME = "L'Anse du Portier"
MT = None                            # {"zone": Polygon, "land": 50 cm mask}
MT_PLATEAU = 5.0                     # m NGF, the esplanade (BD TOPO ground levels 4.5-6)
MT_EDGE = 3.4                        # m NGF, the seafront promenade
MT_WHITE = "#f3f1ec"
MT_PAVING = (214, 206, 190)          # pale limestone paving, as in the street photos


def raster_mask(geom):
    n = int((X1 - X0) / RES)
    img = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(img)
    for q in to_polys(geom):
        d.polygon([((x - X0) / RES, (Y1 - y) / RES) for x, y in q.exterior.coords], fill=255)
        for h in q.interiors:
            d.polygon([((x - X0) / RES, (Y1 - y) / RES) for x, y in h.coords], fill=0)
    return np.asarray(img) > 0


def mareterra_setup(ways, mnt, mns, ortho, sea, land):
    """Replace the 2021 building site with the finished esplanade: ground
    at MT_PLATEAU that eases into the old streets around it and steps down
    to MT_EDGE along the sea, sea where the OpenStreetMap coastline says so
    (the pits read as sea), no cranes in the surface model, and plain
    paving in the photo (so the site's dirt does not turn into lawns)."""
    zone = None
    for t, pts, closed in ways:
        if closed and t.get("place") and t.get("name") == MT_NAME and len(pts) > 3:
            zone = Polygon(pts).buffer(0)
    if zone is None:
        return None
    zm = raster_mask(zone)
    sea[zm] = ~land[zm]
    L = zm & ~sea
    out = ~zm & ~sea & np.isfinite(mnt)
    d_in, (ri, ci) = ndimage.distance_transform_edt(~out, return_indices=True)
    edge = mnt[ri, ci]
    ss = lambda v: v * v * (3 - 2 * v)
    w = ss(np.clip(d_in * RES / 25.0, 0, 1))
    d_sea = ndimage.distance_transform_edt(~sea) * RES
    target = MT_PLATEAU - (MT_PLATEAU - MT_EDGE) * (1 - ss(np.clip((d_sea - 4) / 14.0, 0, 1)))
    z = w * target + (1 - w) * edge
    z = np.where(L, z, np.nan_to_num(mnt, nan=0.0))
    z = ndimage.gaussian_filter(z, 3.0)
    mnt[L] = z[L]
    mns[L] = z[L]
    ortho[L] = MT_PAVING
    return {"zone": zone, "land": L, "mask": zm, "dsea": d_sea}


def mareterra_feats(feats, ways):
    """Mareterra's buildings from OpenStreetMap (mapped after it opened),
    in place of the BD TOPO outlines (surveyed mid-construction)."""
    if MT is None:
        return feats
    zone = MT["zone"]
    keep, bd = [], []
    for f in feats:
        g = shapely.force_2d(shape(f["geometry"])).buffer(0)
        if zone.contains(g.centroid):
            bd.append((g, float(f["properties"].get("hauteur") or 0)))
        else:
            keep.append(f)
    blds, parts = [], []
    for t, pts, closed in ways:
        if not closed or len(pts) < 4:
            continue
        b = t.get("building")
        if not (b or t.get("building:part")) or b in ("roof", "no"):
            continue
        g = Polygon(pts).buffer(0)
        if g.area < 10 or not zone.contains(g.representative_point()):
            continue
        (parts if t.get("building:part") else blds).append((g, t))
    pu = unary_union([g for g, _ in parts]) if parts else None
    n = 0
    for g, t in blds + parts:
        if pu is not None and "building" in t:
            g = g.difference(pu)
        lv = t.get("building:levels", "")
        lv = float(lv) if lv.replace(".", "").isdigit() else None
        hs = t.get("height", "").replace("m", "").strip()
        try:
            h = float(hs)
        except ValueError:
            h = None
        if h is None:
            # The tallest BD TOPO 2024 outline under it, else its floors.
            hb = [hh for gb, hh in bd if gb.intersection(g).area > 0.3 * min(gb.area, g.area)]
            if lv:
                h = lv * 3.3 + 1.0
                if hb and abs(max(hb) - h) < 4:
                    h = max(hb)
            elif hb and max(hb) > 3:
                h = max(hb)
            else:
                h = 11.0 if t.get("building") == "house" else 7.0
        if not lv:
            lv = max(1, round((h - 1.0) / 3.3))
        for q in to_polys(g):
            if q.area < 10:
                continue
            n += 1
            keep.append({"type": "Feature", "geometry": shapely.geometry.mapping(q),
                         "properties": {"cleabs": f"MARETERRA{n:03d}", "hauteur": round(h, 1),
                                        "date_creation": "2024-12-04", "mareterra": {
                                            "levels": lv, "name": t.get("name"),
                                            "house": t.get("building") in ("house", "terrace")}}})
    print(f"Mareterra: {n} buildings from OpenStreetMap in place of {len(bd)} BD TOPO outlines")
    return keep


def mareterra_terraces(m, poly, base, H, fh, mt):
    """The deep white slab balconies that wrap Mareterra's blocks (seen in
    the 2025 street photos): one slab per floor, a thin white edge with a
    glass rail on top, the underside a shade darker."""
    if H < 6:
        return 0
    depth = 1.0 if H > 40 else 1.7
    white = np.array(hexrgb(MT_WHITE))
    rail = np.array(hexrgb("#a9bcc0"))
    js = 1 if mt.get("house") else 2
    slab = poly.buffer(depth, join_style=js, quad_segs=3)
    if not isinstance(slab, Polygon):
        return 0
    slab = orient(slab, 1.0)
    ring = [orient(q, 1.0) for q in to_polys(slab.difference(poly))]
    k_ = 0
    nf = int(round((H - 1.0) / fh))
    for k in range(1, nf + 1):
        z = base + k * fh
        if z > base + H - 0.6:
            break
        add_walls(m, slab, z - 0.38, z, z, 10, white, PLAIN)
        for q in ring:
            add_roof(m, q, z, white * 0.97, False)
            add_roof(m, orient(q, -1.0), z - 0.38, white * 0.8, False)
        add_walls(m, slab, z, z + 1.05, z, 10, rail, PLAIN)
        k_ += 1
    return k_


def mareterra_trees(m, ways, mnt, bmask, stats):
    """Planted pines, olives and shrubs in Mareterra's park and gardens
    (the LiDAR predates them): a jittered grid inside each green area."""
    if MT is None:
        return 0
    zone = MT["zone"]
    rnd = np.random.default_rng(24)
    mntR = Raster(mnt)
    DENS = {"park": 9.0, "garden": 8.0, "scrub": 7.0, "wood": 7.0, "forest": 7.0, "grass": 15.0,
            "meadow": 13.0, "flowerbed": 99.0}
    n = 0
    for t, pts, closed in ways:
        k = t.get("leisure") or t.get("landuse") or t.get("natural")
        if not closed or k not in DENS or len(pts) < 4:
            continue
        g = Polygon(pts).buffer(0)
        if not zone.contains(g.representative_point()):
            continue
        sp = DENS[k]
        x0, y0, x1, y1 = g.bounds
        for x in np.arange(x0 + sp / 2, x1, sp):
            for y in np.arange(y0 + sp / 2, y1, sp):
                px_, py_ = x + rnd.uniform(-0.4, 0.4) * sp, y + rnd.uniform(-0.4, 0.4) * sp
                if not g.buffer(-1.5).contains(Point(px_, py_)):
                    continue
                r_, c_ = int((Y1 - py_) / RES), int((px_ - X0) / RES)
                if not (0 <= r_ < bmask.shape[0] and 0 <= c_ < bmask.shape[1]) or bmask[r_, c_] \
                        or not MT["land"][r_, c_]:
                    continue
                z = float(mntR.sample(np.array([px_]), np.array([py_]))[0])
                if not np.isfinite(z):
                    continue
                u = rnd.random()
                if u < 0.55:
                    add_tree(m, px_, py_, z, rnd.uniform(7, 11), rnd.uniform(2.6, 3.8), "pine", rnd)
                elif u < 0.85:
                    add_tree(m, px_, py_, z, rnd.uniform(4, 6.5), rnd.uniform(1.8, 2.6), "broad", rnd)
                else:
                    add_tree(m, px_, py_, z, rnd.uniform(1.2, 2.0), rnd.uniform(0.9, 1.4), "broad", rnd)
                n += 1
    # The seafront promenade: a row of umbrella pines 8 m in from the edge.
    pools = unary_union([Polygon(p_).buffer(2.0) for t, p_, c in ways
                         if c and len(p_) > 3 and (t.get("leisure") == "swimming_pool" or t.get("natural") == "water")])
    ring = zone.buffer(-8.0)
    for q in to_polys(ring):
        ln = q.exterior
        for s_ in np.arange(0, ln.length, 12.0):
            pt = ln.interpolate(s_)
            r_, c_ = int((Y1 - pt.y) / RES), int((pt.x - X0) / RES)
            if not (0 <= r_ < bmask.shape[0] and 0 <= c_ < bmask.shape[1]):
                continue
            if not MT["land"][r_, c_] or bmask[r_, c_] or not (5.0 < MT["dsea"][r_, c_] < 13.0) \
                    or pools.contains(pt):
                continue
            z = float(mntR.sample(np.array([pt.x]), np.array([pt.y]))[0])
            if np.isfinite(z):
                add_tree(m, pt.x, pt.y, z, rnd.uniform(8, 10.5), rnd.uniform(3.0, 3.8), "pine", rnd)
                n += 1
    stats["mareterra_trees"] = n
    return n


# ----------------------------------------------------------------- tunnel ----
# The Grand Prix tunnel runs under the Fairmont hotel. The LiDAR ground model
# fills it in (the scan only sees its roof), so the set carves it out: the
# ground over it is lifted to a deck above the vault, buildings start above
# it, and an open tube with a road, walls and a ceiling is built inside.
# The centre line and road height are the lap tours' (viewer/lap-route.js),
# so the tours' own lit tunnel (viewer/clean-track.js) fits inside.
TUNNEL = None
TUBE_HALF, TUBE_H = 6.7, 6.5
# The Grand Prix track the tours and Drive run on (viewer/clean-track.js
# models it 5 m either side of the centre line, barriers at 5.35 m). Nothing
# of the set may stand on it, and the ground may not rise over its edges.
TRACK_HALF = 5.0
GROUND_UNDER = 0.3                    # m, the ground under the circuit's road
TRACK = None


def load_track():
    f = ROOT / "viewer" / "lap-route.js"
    if not f.exists():
        return None
    txt = f.read_text()
    d = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    P = np.array(d["points"])
    tl = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    x, y = tl.transform(P[:, 0], P[:, 1])
    x = ndimage.gaussian_filter1d(np.asarray(x), 2, mode="wrap")
    y = ndimage.gaussian_filter1d(np.asarray(y), 2, mode="wrap")
    z = ndimage.gaussian_filter1d(P[:, 2] - GEOID_N, 3, mode="wrap")
    line = LineString(np.column_stack([np.append(x, x[0]), np.append(y, y[0])]))
    from scipy.spatial import cKDTree
    return {"x": x, "y": y, "z": z, "line": line, "kd": cKDTree(np.column_stack([x, y])),
            "step": float(d.get("step", 2.0)),
            "keep_out": line.buffer(TRACK_HALF + 0.8)}


# Avenue d'Ostende, the climb from Sainte-Dévote to the Casino (lap metres):
# on its sea side a brick pavement and a stone parapet over the drop to
# the port, as in the street photos.
OSTENDE = (232, 652)
OSTENDE_SIDE = -1                     # the sea is on the right going up
PROM = None
PROM_WALL = 25.0                      # m, the most retaining wall shown below the parapet


def ostende_setup(mnt):
    """Where the parapet stands, lap point by lap point: at the edge of the
    drop the LiDAR sees beside the road, between 1.6 and 4.8 m past the
    track's edge."""
    t = TRACK
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    i0, i1 = int(OSTENDE[0] / step), int(OSTENDE[1] / step) + 1
    idx = np.arange(i0, i1)
    dx = np.gradient(t["x"]); dy = np.gradient(t["y"])
    ln = np.hypot(dx, dy) + 1e-9
    nx, ny = OSTENDE_SIDE * -dy / ln, OSTENDE_SIDE * dx / ln
    R = Raster(mnt)
    pd = np.full(len(idx), TRACK_HALF + 4.5)
    for k, i in enumerate(idx):
        for d in np.arange(TRACK_HALF + 1.0, TRACK_HALF + 6.0, 0.25):
            g = R.sample(np.array([t["x"][i] + nx[i] * d]), np.array([t["y"][i] + ny[i] * d]))[0]
            if not np.isfinite(g) or g < t["z"][i] - 0.8:
                pd[k] = d - 0.5
                break
    pd = np.clip(pd, TRACK_HALF + 1.6, TRACK_HALF + 4.8)
    pd = ndimage.uniform_filter1d(ndimage.median_filter(pd, 9, mode="nearest"), 7, mode="nearest")
    # The lowest ground within 3 m: below the parapet the 3 m ground grid
    # would otherwise mix the road's level and the drop into spikes.
    hmin = ndimage.minimum_filter(np.where(np.isfinite(mnt), mnt, 1e3), 13)
    # And within 6 m, for the needles further down the slope.
    hmin2 = ndimage.minimum_filter(hmin, 13)
    return {"hmin2": hmin2, "idx": idx, "pd": pd, "nx": nx[idx], "ny": ny[idx], "i0": i0, "i1": i1, "hmin": hmin}


def ostende_promenade(m, mnt, stats):
    """The sea side of Avenue d'Ostende: red brick pavement from behind the
    barrier to a stone parapet a metre high, its outer face a retaining
    wall down to the slope below, with a pale coping on top."""
    if PROM is None:
        return 0
    t, P_ = TRACK, PROM
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    idx, pd, nx, ny = P_["idx"], P_["pd"], P_["nx"], P_["ny"]
    x, y, z = t["x"][idx], t["y"][idx], t["z"][idx]
    R = Raster(mnt)
    brick = np.array(hexrgb("#93604f"))
    # Warm limestone, a slightly different tint every few metres of wall
    # (stones laid in runs), so the long wall does not read as one slab.
    tint = 0.95 + 0.08 * np.random.default_rng(7).random(len(idx) - 1)[:, None]
    stone = np.array(hexrgb("#e2d6bc"))[None] * tint
    cope = np.array(hexrgb("#efe7d6"))
    d0 = TRACK_HALF + 0.1                 # from the kerb, as the pavements elsewhere (SW_START)

    def at(d, zz):
        return enu_fast(x + nx * d, y + ny * d, zz)
    # Facing outward (right of travel for OSTENDE_SIDE -1): triangles
    # wound as pavements() does for side -1.
    pi, po = at(d0, z + 0.12), at(pd, z + 0.12)
    a, b, c, d_ = pi[:-1], pi[1:], po[1:], po[:-1]
    # Both faces: the pavement must show whichever way it is wound.
    m.tri(a, b, c, brick, PLAIN); m.tri(a, c, d_, brick, PLAIN)
    m.tri(a, c, b, brick, PLAIN); m.tri(a, d_, c, brick, PLAIN)
    # The kerb's face down to the road.
    kl, kh = at(d0, z - 0.3), at(d0, z + 0.12)
    kc = np.array(hexrgb("#bdb5a6"))
    a, b, c, d_ = kl[:-1], kl[1:], kh[1:], kh[:-1]
    m.tri(a, b, c, kc, PLAIN); m.tri(a, c, d_, kc, PLAIN)
    m.tri(a, c, b, kc, PLAIN); m.tri(a, d_, c, kc, PLAIN)
    # Ground beyond the parapet (the lowest within 3 m, as ground_mesh()
    # lowers it): the foot of the retaining wall.
    gx, gy = x + nx * (pd + 2.0), y + ny * (pd + 2.0)
    g = Raster(PROM["hmin"]).sample(gx, gy)
    foot = np.minimum(np.where(np.isfinite(g) & (g < 900), g, z - 6), z - 0.5)
    # The wall goes down to the slope below (on the 3 m grid a steep
    # slope would be a curtain of stretched photo).
    foot = np.maximum(foot, z - PROM_WALL) - 0.5
    foot = ndimage.minimum_filter1d(foot, 3)
    top = z + 0.95
    w = 0.45
    inner_lo, inner_hi = at(pd, z + 0.12), at(pd, top)
    outer_lo, outer_hi = at(pd + w, foot), at(pd + w, top)

    def quad(p0, p1, p2, p3, col):     # p0-p1 bottom run, p3-p2 top run
        A, B, C, D = p0[:-1], p0[1:], p2[1:], p2[:-1]
        m.tri(A, B, C, col, PLAIN); m.tri(A, C, D, col, PLAIN)
    # Inner face (seen from the road) and outer face, each wound to face
    # its own way.
    A, B, C, D = inner_lo[:-1], inner_lo[1:], inner_hi[1:], inner_hi[:-1]
    m.tri(A, C, B, stone, PLAIN); m.tri(A, D, C, stone, PLAIN)
    A, B, C, D = outer_lo[:-1], outer_lo[1:], outer_hi[1:], outer_hi[:-1]
    m.tri(A, B, C, stone * 0.92, PLAIN); m.tri(A, C, D, stone * 0.92, PLAIN)
    # Coping: a slab a little wider than the wall.
    ci, co = at(pd - 0.06, top + 0.08), at(pd + w + 0.06, top + 0.08)
    A, B, C, D = ci[:-1], ci[1:], co[1:], co[:-1]
    m.tri(A, B, C, cope, PLAIN); m.tri(A, C, D, cope, PLAIN)
    ci0, co0 = at(pd - 0.06, top), at(pd + w + 0.06, top)
    A, B, C, D = ci0[:-1], ci0[1:], ci[1:], ci[:-1]
    m.tri(A, C, B, cope * 0.9, PLAIN); m.tri(A, D, C, cope * 0.9, PLAIN)
    A, B, C, D = co0[:-1], co0[1:], co[1:], co[:-1]
    m.tri(A, B, C, cope * 0.9, PLAIN); m.tri(A, C, D, cope * 0.9, PLAIN)
    # The retaining wall's masonry: a pilaster every 6 m and a string
    # course a metre under the coping.
    for k in range(2, len(idx) - 2, max(1, int(round(6 / step)))):
        if top[k] - foot[k] < 2.5:
            continue
        ux, uy = -ny[k], nx[k]
        cx, cy = x[k] + nx[k] * (pd[k] + w), y[k] + ny[k] * (pd[k] + w)
        q = Polygon([(cx - ux * 0.35, cy - uy * 0.35), (cx + ux * 0.35, cy + uy * 0.35),
                     (cx + ux * 0.35 + nx[k] * 0.3, cy + uy * 0.35 + ny[k] * 0.3),
                     (cx - ux * 0.35 + nx[k] * 0.3, cy - uy * 0.35 + ny[k] * 0.3)])
        add_walls(m, orient(q, 1.0), foot[k], top[k] - 1.2, foot[k], 100, np.array(hexrgb("#d8ccb2")), PLAIN)
        add_roof(m, orient(q, 1.0), top[k] - 1.2, np.array(hexrgb("#d8ccb2")), False)
    sc_lo, sc_hi = at(pd + w, top - 1.0), at(pd + w + 0.15, top - 1.0)
    sc_tlo, sc_thi = at(pd + w, top - 0.8), at(pd + w + 0.15, top - 0.8)
    A, B, C, D = sc_lo[:-1], sc_lo[1:], sc_hi[1:], sc_hi[:-1]
    m.tri(A, B, C, cope * 0.85, PLAIN); m.tri(A, C, D, cope * 0.85, PLAIN)
    m.tri(A, C, B, cope * 0.85, PLAIN); m.tri(A, D, C, cope * 0.85, PLAIN)
    A, B, C, D = sc_hi[:-1], sc_hi[1:], sc_thi[1:], sc_thi[:-1]
    m.tri(A, B, C, cope * 0.9, PLAIN); m.tri(A, C, D, cope * 0.9, PLAIN)
    A, B, C, D = sc_tlo[:-1], sc_tlo[1:], sc_thi[1:], sc_thi[:-1]
    m.tri(A, B, C, cope * 0.95, PLAIN); m.tri(A, C, D, cope * 0.95, PLAIN)
    m.tri(A, C, B, cope * 0.95, PLAIN); m.tri(A, D, C, cope * 0.95, PLAIN)
    # Lamp posts on the parapet every 25 m, a pair of lanterns on each,
    # as along the avenue.
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    post_c, head_c = np.array(hexrgb("#262c29")), np.array(hexrgb("#fff1c8"))
    tx = np.gradient(x); ty = np.gradient(y); tl = np.hypot(tx, ty) + 1e-9
    for k in range(4, len(idx) - 4, int(round(25 / step))):
        px, py = x[k] + nx[k] * (pd[k] + w / 2), y[k] + ny[k] * (pd[k] + w / 2)
        zb = top[k] + 0.08
        add_walls(m, box(px - 0.18, py - 0.18, px + 0.18, py + 0.18), zb, zb + 0.5, zb, 10, post_c, PLAIN)
        add_walls(m, box(px - 0.07, py - 0.07, px + 0.07, py + 0.07), zb + 0.5, zb + 3.6, zb, 10, post_c, PLAIN, min_edge=0.05)
        ux, uy = tx[k] / tl[k], ty[k] / tl[k]
        arm = LineString([(px - ux * 0.55, py - uy * 0.55), (px + ux * 0.55, py + uy * 0.55)]).buffer(0.04, cap_style=2)
        add_walls(m, orient(arm, 1.0), zb + 3.45, zb + 3.53, zb, 10, post_c, PLAIN)
        for sgn in (-1, 1):
            hx, hy = px + sgn * ux * 0.55, py + sgn * uy * 0.55
            head = box(hx - 0.16, hy - 0.16, hx + 0.16, hy + 0.16)
            add_walls(m, head, zb + 3.0, zb + 3.45, zb, 10, head_c, PLAIN, style=LAMP_HEAD)
            add_roof(m, box(hx - 0.2, hy - 0.2, hx + 0.2, hy + 0.2), zb + 3.5, post_c, False)
    stats["ostende_parapet_m"] = int(len(idx) * step)
    return len(idx)


def flatten_harbour(mnt):
    """Along the harbour stretch of the circuit (below 8 m, after the
    tunnel) the quays are flat; the LiDAR, flown in spring 2021, caught the
    Grand Prix grandstands there as humps of "ground". Within 45 m of that
    stretch the ground may rise no more than 3 cm per metre above the track."""
    t = TRACK
    n = len(t["x"])
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    s_ = np.arange(n) * step
    tb = TUNNEL["tb"] if TUNNEL is not None else -1
    sel = (t["z"] < 8.0) & (s_ > tb + 30)
    if sel.sum() < 10:
        return mnt
    from scipy.spatial import cKDTree
    kd = cKDTree(np.column_stack([t["x"][sel], t["y"][sel]]))
    zt_all = t["z"][sel]
    rows, cols = np.nonzero(np.isfinite(mnt))
    cx, cy = cell_xy(rows, cols)
    d, j = kd.query(np.column_stack([cx, cy]), distance_upper_bound=45)
    near = np.isfinite(d)
    out = mnt.copy()
    zr, zc = rows[near], cols[near]
    cap = zt_all[j[near]] + 0.5 + 0.03 * np.maximum(0, d[near] - 6)
    # Not on the hillside of a higher part of the lap: Avenue d'Ostende
    # climbs right above the harbour stretch, and capped to the quay its
    # pavements and the ground up to its buildings sank by up to 28 m.
    hi = t["z"] >= 8.0
    if TUNNEL is not None:
        hi &= ~((s_ > TUNNEL["ta"] - 30) & (s_ < tb + 30))   # (not the tunnel's exit ramp down to the harbour)
    if hi.any():
        dh, _ = cKDTree(np.column_stack([t["x"][hi], t["y"][hi]])).query(
            np.column_stack([cx[near], cy[near]]), distance_upper_bound=30)
        cap = np.where(np.isfinite(dh) & (dh < d[near]), np.inf, cap)
    out[zr, zc] = np.minimum(out[zr, zc], cap)
    return out


# Port Hercule's quays, jetties and car parks (lon/lat box). The 2021 LiDAR
# was flown while Grand Prix stands, cabins and cranes stood there.
PORT_BOX = (7.4205, 43.7325, 7.4285, 43.7372)


def flatten_port(mnt, sea):
    """Away from the circuit too, the port's quays and car parks are flat:
    any isolated hump of LiDAR "ground" (more than 1.5 m above the quay
    around it, with flat surroundings) is levelled to the quay."""
    tl = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    x0, y0 = tl.transform(PORT_BOX[0], PORT_BOX[1])
    x1, y1 = tl.transform(PORT_BOX[2], PORT_BOX[3])
    c0, c1 = int((x0 - X0) / RES), int((x1 - X0) / RES)
    r0, r1 = int((Y1 - y1) / RES), int((Y1 - y0) / RES)
    A = mnt[r0:r1, c0:c1]
    land = np.isfinite(A)
    F = np.where(land, A, 0.8)
    k = 4                                   # work at 2 m: percentile filters are slow
    h, w = F.shape[0] // k * k, F.shape[1] // k * k
    Fs = F[:h, :w].reshape(h // k, k, w // k, k).min(axis=(1, 3))
    up = lambda a: np.pad(np.kron(a, np.ones((k, k))), ((0, F.shape[0] - h), (0, F.shape[1] - w)), mode="edge")
    quay = up(ndimage.percentile_filter(Fs, 20, size=15))       # 30 m
    around = up(ndimage.percentile_filter(Fs, 75, size=31))     # 60 m
    near_sea = ndimage.distance_transform_edt(~sea[r0:r1, c0:c1]) * RES < 90
    hump = land & near_sea & (quay < 4.0) & (F - quay > 1.5) & (around < quay + 2.5)
    hump = ndimage.binary_dilation(hump, iterations=3) & land
    out = mnt.copy()
    sub = out[r0:r1, c0:c1]
    sub[hump] = np.minimum(sub[hump], quay[hump] + 0.15)
    sub[:] = np.where(hump, ndimage.uniform_filter(np.nan_to_num(sub, nan=0.8), 5), sub)
    return out, int(hump.sum() * RES * RES)


# --------------------------------------------------------- Quai Albert 1er ----
# Between Boulevard Albert 1er (the start straight) and the harbour road
# round the swimming pool, Port Hercule is built on two levels: an upper
# esplanade level with the boulevard (OSM pedestrian area, asphalt), with
# its row of stone pines in pale stone planters along the kerb, and the
# quay below it (Route de la Piscine, the Rainier III pool deck), behind a
# stone retaining wall with a parapet and flights of steps (OSM
# barrier=retaining_wall, highway=steps). The 2021 LiDAR was flown while
# the Grand Prix pit buildings and stands stood on the esplanade: levelled
# by flatten_harbour() they became a ramp between the two levels, drawn as
# bare limestone with grass under the pines. Here the esplanade is rebuilt
# flat at the boulevard's level with a vertical wall down to the quay.
QA = None
QA_CENTRE = (7.42189, 43.73505)        # by the esplanade (OSM way 941748453)
QA_STRAIGHT = [(3080, 3320), (0, 220)]  # lap metres along Boulevard Albert 1er
QA_ASPHALT = "#8f6658"                 # the esplanade: red-brown like the walk under the pines (Fly, 2026-11-20)
QA_WALK = "#8f6658"                    # the walk under the pines: red-brown asphalt
QA_WALK_W = 6.0                        # m, from the boulevard's pavement edge
QA_DECK = "#e3ddd0"                    # the pool deck: pale stone
QA_STONE, QA_COPING, QA_FACE = "#e2dbcb", "#efe9dc", "#cfc6b3"


def quai_setup(ways, mnt, sea):
    """Finds the esplanade and the boulevard beside it, sets the terrain
    model to the two levels (the esplanade at the boulevard's kerb height,
    the ground just outside its drops at the quay's height) and returns
    what the mesh, the paint and quai_albert() need."""
    if TRACK is None:
        return None
    tl = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    c = Point(*tl.transform(*QA_CENTRE))
    area = None
    for t, pts, closed in ways:
        if closed and t.get("highway") == "pedestrian" and t.get("area") == "yes" and len(pts) > 8:
            g = Polygon(pts).buffer(0)
            if g.area > 2000 and g.distance(c) < 25 and (area is None or g.area > area.area):
                area = g
    if area is None:
        print("Quai Albert 1er: esplanade not found in OSM")
        return None
    t = TRACK
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    s_ = np.arange(len(t["x"])) * step
    on = np.zeros(len(s_), bool)
    for a, b in QA_STRAIGHT:
        on |= (s_ >= a) & (s_ <= b)
    from scipy.spatial import cKDTree
    kd = cKDTree(np.column_stack([t["x"][on], t["y"][on]]))
    zs = t["z"][on]
    # The boulevard's carriageway and pavements stay out (pavements()
    # lays a stone strip to HALF + 2.6 m); the esplanade starts there.
    pieces = [LineString(np.column_stack([t["x"][on][i0:i1], t["y"][on][i0:i1]]))
              for i0, i1 in _runs(on)]
    road = unary_union([p.buffer(TRACK_HALF + 2.6) for p in pieces])
    terr = area.difference(road)
    # The rest of the lap (the harbour road) keeps its own pavements.
    terr = terr.difference(t["line"].buffer(TRACK_HALF + 2.6))
    terr = max(to_polys(terr), key=lambda q: q.area) if not terr.is_empty else None
    if terr is None or terr.area < 1000:
        return None
    # Straight across the wall's shallow recesses (up to 8 m wide): left
    # in, the 3 m ground grid humped up in them at the foot of the wall.
    closed_ = terr.buffer(4.0, join_style=2).buffer(-4.0, join_style=2)
    closed_ = closed_.difference(road).difference(t["line"].buffer(TRACK_HALF + 2.6))
    pools_ = [Polygon(p_).buffer(1.0) for tg, p_, cl in ways if cl and len(p_) > 3
              and tg.get("leisure") == "swimming_pool"]
    if pools_:
        closed_ = closed_.difference(unary_union(pools_))
    if not closed_.is_empty:
        terr = max(to_polys(unary_union([terr, closed_])), key=lambda q: q.area)
    terr = orient(terr.simplify(0.25), 1.0)

    def zup(x, y):
        d, j = kd.query(np.column_stack([np.atleast_1d(x), np.atleast_1d(y)]))
        return zs[j] + 0.14

    # 50 cm masks: inside, and a 4 m band outside.
    ins = raster_mask(terr)
    band = raster_mask(terr.buffer(4.0)) & ~ins & ~sea
    rr, cc = np.nonzero(ins)
    xs, ys = cell_xy(rr, cc)
    zu_cells = zup(xs, ys)
    # The quay level below: LiDAR ground around the esplanade at least
    # 1.2 m below it (not the esplanade, not the stands on it), spread
    # into it from the nearest such cell and smoothed; along the quay
    # road no lower than the road (the 2021 scan has patches of "ground"
    # near sea level at the foot of the wall).
    near = raster_mask(terr.buffer(25.0))
    nr_, nc_ = np.nonzero(near)
    nx_, ny_ = cell_xy(nr_, nc_)
    zu_near = np.full(mnt.shape, np.nan, np.float32)
    zu_near[nr_, nc_] = zup(nx_, ny_)
    cand = near & ~ins & ~sea & np.isfinite(mnt) & (mnt < zu_near - 1.2)
    if cand.sum() < 100:
        return None
    idx = ndimage.distance_transform_edt(~cand, return_distances=False, return_indices=True)
    low = np.where(cand, mnt, np.nan)[idx[0], idx[1]]
    low = ndimage.percentile_filter(low, 25, size=13)
    dtr, jt = TRACK["kd"].query(np.column_stack([nx_, ny_]))
    lz = low[nr_, nc_]
    by_road = dtr < 14.0
    lz[by_road] = np.maximum(lz[by_road], TRACK["z"][jt[by_road]] + 0.1)
    low_full = np.full(mnt.shape, np.nan, np.float32)
    low_full[nr_, nc_] = lz
    lowR = Raster(low_full)
    # Which parts of the outline drop to a lower quay (> 0.8 m below).
    ring = np.asarray(terr.exterior.coords)[:, :2]
    dense = []
    for a, b in zip(ring[:-1], ring[1:]):
        n = max(1, int(math.ceil(np.linalg.norm(b - a) / 1.0)))
        for k in range(n):
            dense.append(a + (b - a) * k / n)
    dense = np.array(dense + [ring[-1]])
    a, b = dense[:-1], dense[1:]
    L = np.linalg.norm(b - a, axis=1) + 1e-9
    u = (b - a) / L[:, None]
    out = np.column_stack([u[:, 1], -u[:, 0]])          # ccw ring: right side is outside
    mid = (a + b) / 2
    zo = lowR.sample(mid[:, 0] + out[:, 0] * 2.0, mid[:, 1] + out[:, 1] * 2.0)
    zi = zup(mid[:, 0], mid[:, 1])
    # Only on the harbour side: away from the boulevard (on its side the
    # LiDAR pavements were capped by flatten_harbour() from the quay road).
    dmid = kd.query(mid)[0]
    dout = kd.query(mid + out * 2.0)[0]
    drop = np.isfinite(zo) & (zi - zo > 0.8) & (dout > dmid + 1.0)
    # Lone short runs are noise.
    lab, nr = ndimage.label(drop)
    for r in range(1, nr + 1):
        if (lab == r).sum() < 4:
            drop[lab == r] = False
    # The wall steps in and out every 15 m or so (OSM way 856532999):
    # the short returns between two drops are wall too.
    lab, nr = ndimage.label(~drop)
    for r in range(1, nr + 1):
        if (lab == r).sum() < 7:
            drop[lab == r] = True
    segs = [LineString([a[i], b[i]]) for i in np.nonzero(drop)[0]]
    drop_line = unary_union(segs) if segs else None
    # Terrain: the esplanade flat; outside its drops, the quay.
    mnt[rr, cc] = zu_cells
    if drop_line is not None:
        br, bc = np.nonzero(band)
        bx, by = cell_xy(br, bc)
        near = shapely.distance(drop_line, shapely.points(bx, by)) < 4.0
        lo = lowR.sample(bx, by)
        ok = near & np.isfinite(lo)
        mnt[br[ok], bc[ok]] = np.minimum(mnt[br[ok], bc[ok]], lo[ok])
    walk = terr.intersection(road.buffer(QA_WALK_W))
    print(f"Quai Albert 1er: esplanade {terr.area:.0f} m2, retaining wall {sum(s.length for s in segs):.0f} m")
    return {"poly": terr, "zup": zup, "drop": drop_line, "low": lowR, "mask": ins, "walk": walk,
            "segs": (a[drop], b[drop], out[drop]), "kd": kd, "on": np.nonzero(on)[0]}


def _runs(mask):
    """(start, end) index pairs of the True runs of a 1-D mask."""
    lab, n = ndimage.label(mask)
    return [(int(np.nonzero(lab == r)[0][0]), int(np.nonzero(lab == r)[0][-1]) + 1) for r in range(1, n + 1)]


def quai_ground(X, Y, Z):
    """The 3 m ground grid under the esplanade: next to the drops at the
    quay's height (so no ground triangle ramps out from the foot of the
    wall), elsewhere just under the esplanade's own slab. Returns the
    mesh heights and the heights other things stand on."""
    if QA is None:
        return Z, Z
    ins = contains_xy(QA["poly"], X, Y)
    if not ins.any():
        return Z, Z
    zu = QA["zup"](X[ins], Y[ins])
    Zm, Zs = Z.copy(), Z.copy()
    Zs[ins] = zu
    if QA["drop"] is not None:
        d = shapely.distance(QA["drop"], shapely.points(X[ins], Y[ins]))
        lo = QA["low"].sample(X[ins], Y[ins])
        lo = np.where(np.isfinite(lo), lo, zu - 2.0)
        Zm[ins] = np.where(d < 4.5, np.minimum(lo, zu - 0.3), zu - 0.12)
    else:
        Zm[ins] = zu - 0.12
    # Ground nodes just outside a drop: the quay.
    if QA["drop"] is not None:
        out = ~ins & contains_xy(QA["poly"].buffer(4.5), X, Y)
        d = shapely.distance(QA["drop"], shapely.points(X[out], Y[out]))
        lo = QA["low"].sample(X[out], Y[out])
        # In the wall's recesses too, never above the quay road beside it
        # (a lump of ground stood in one, up to the top of the wall).
        dq, jq = TRACK["kd"].query(np.column_stack([X[out], Y[out]]))
        lo = np.where(dq < 25.0, np.minimum(lo, TRACK["z"][jq] + 0.1), lo)
        sel = (d < 4.5) & np.isfinite(lo)
        zz = Zm[out]
        zz[sel] = np.minimum(zz[sel], lo[sel])
        Zm[out] = zz
        zs2 = Zs[out]
        zs2[sel] = np.minimum(zs2[sel], lo[sel])
        Zs[out] = zs2
    return Zm, Zs


def _stairs(m, a, b, za, zb, w, col, side_col):
    """A straight flight from a (za) down to b (zb), w wide, solid to the
    ground below, with a low stone wall on each side."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    L = float(np.linalg.norm(b - a))
    ax = (b - a) / L
    n = max(2, int(round(abs(za - zb) / 0.16)))
    for k in range(n):
        c = a + ax * L * (k + 0.5) / n
        z1 = za + (zb - za) * (k + 0.5) / n + 0.08
        _box(m, c[0], c[1], ax, L / n + 0.02, w, min(za, zb) - 0.5, z1, col)
    sd = np.array([-ax[1], ax[0]])
    for s in (-1, 1):
        for k in range(n):
            c = a + ax * L * (k + 0.5) / n + sd * s * (w / 2 + 0.15)
            z1 = za + (zb - za) * (k + 0.5) / n + 0.9
            _box(m, c[0], c[1], ax, L / n + 0.02, 0.3, min(za, zb) - 0.5, z1, side_col)


def _quad(m, p0, p1, p2, p3, col, want):
    """Quads p0-p1-p2-p3 (arrays of points) as two triangles each, wound
    so that they face along `want` (k x 3, model axes)."""
    for a, b, c in ((p0, p1, p2), (p0, p2, p3)):
        n = np.cross(b - a, c - a)
        flip = (n * want).sum(1) < 0
        b2 = np.where(flip[:, None], c, b)
        c2 = np.where(flip[:, None], b, c)
        m.tri(a, b2, c2, col, PLAIN)


def quai_albert(gm, m, tm, ways, pts, mntR, stats, fps=()):
    """The esplanade's slab (painted from the ground map), the retaining
    wall with its parapet and coping, the steps down to the quay and the
    stone pines in their planters along the boulevard."""
    if QA is None:
        return 0
    terr = QA["poly"]
    blds = unary_union([p for p in fps if p.intersects(terr)]) if fps else None
    slab = terr.difference(blds) if blds is not None and not blds.is_empty else terr
    for pg in to_polys(slab):
        if pg.area < 2:
            continue
        tr = triangulate(orient(pg, 1.0), area=6)
        if tr is None:
            continue
        v, tri = tr
        z = QA["zup"](v[:, 0], v[:, 1])
        P = enu_fast(v[:, 0], v[:, 1], z)
        uv = np.column_stack([(v[:, 0] - X0) / (X1 - X0), (Y1 - v[:, 1]) / (Y1 - Y0)])
        gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=tri)
    # Steps that leave the esplanade over a drop.
    stair_heads = []
    nst = 0
    for t, p_, closed in ways:
        if t.get("highway") != "steps" or closed or len(p_) < 2:
            continue
        ls = LineString(p_)
        if ls.distance(terr) > 2.0 or ls.length < 3:
            continue
        a, b = np.array(p_[0], float), np.array(p_[-1], float)
        ina, inb = terr.buffer(0.5).contains(Point(*a)), terr.buffer(0.5).contains(Point(*b))
        if ina == inb:
            continue
        top, bot = (a, b) if ina else (b, a)
        # The flight starts at the wall line.
        cut = ls.intersection(terr.exterior)
        if not cut.is_empty:
            q = cut if cut.geom_type == "Point" else min(getattr(cut, "geoms", [cut]), key=lambda g: g.distance(Point(*top)))
            top = np.array([q.x, q.y]) if q.geom_type == "Point" else top
        zt = float(QA["zup"](top[0], top[1])[0])
        zb_ = float(np.nan_to_num(QA["low"].sample(np.array([bot[0]]), np.array([bot[1]]))[0], nan=zt - 2))
        zb_ = max(zb_, float(np.nan_to_num(mntR.sample(np.array([bot[0]]), np.array([bot[1]]))[0], nan=zb_)))
        if zt - zb_ < 0.5 or np.linalg.norm(bot - top) < 2:
            continue
        w = float(t.get("width", 3.2)) if str(t.get("width", "")).replace(".", "").isdigit() else 3.2
        _stairs(m, top, bot, zt, zb_, w, np.array(hexrgb(QA_STONE)), np.array(hexrgb(QA_FACE)))
        stair_heads.append((top, w))
        nst += 1
    # The wall: face from the quay up to the esplanade, then a 0.9 m
    # parapet with a coping, left open at the head of each flight.
    a, b, out = QA["segs"]
    if len(a):
        mid = (a + b) / 2
        keep = np.ones(len(a), bool)
        for h, w in stair_heads:
            keep &= np.hypot(mid[:, 0] - h[0], mid[:, 1] - h[1]) > w / 2 + 0.2
        if blds is not None and not blds.is_empty:
            keep &= ~contains_xy(blds.buffer(0.3), mid[:, 0], mid[:, 1])
        keep &= track_dist(mid[:, 0], mid[:, 1]) > TRACK_HALF + 1.5
        a, b, out, mid = a[keep], b[keep], out[keep], mid[keep]
        za, zb = QA["zup"](a[:, 0], a[:, 1]), QA["zup"](b[:, 0], b[:, 1])
        la = QA["low"].sample(a[:, 0] + out[:, 0] * 2, a[:, 1] + out[:, 1] * 2)
        la = np.where(np.isfinite(la), la, za - 2.5) - 0.4
        lb = QA["low"].sample(b[:, 0] + out[:, 0] * 2, b[:, 1] + out[:, 1] * 2)
        lb = np.where(np.isfinite(lb), lb, zb - 2.5) - 0.4
        face, stone, coping = (np.array(hexrgb(c)) for c in (QA_FACE, QA_STONE, QA_COPING))
        k = len(a)
        o3 = np.column_stack([out, np.zeros(k)])
        up = np.tile([0.0, 0.0, 1.0], (k, 1))
        P = lambda p, z: enu_fast(p[:, 0], p[:, 1], z)
        Hp = 0.9
        ai, bi = a - out * 0.35, b - out * 0.35
        ao, bo = a + out * 0.06, b + out * 0.06
        ai2, bi2 = a - out * 0.41, b - out * 0.41
        # Retaining wall face, flush with the outline, up to the parapet top.
        _quad(m, P(a, la), P(b, lb), P(b, zb + Hp), P(a, za + Hp), face, o3)
        # Parapet's inner face (0.35 m thick).
        _quad(m, P(ai, za), P(bi, zb), P(bi, zb + Hp), P(ai, za + Hp), stone, -o3)
        # Coping: a pale slab overhanging both faces.
        zc_a, zc_b = za + Hp + 0.1, zb + Hp + 0.1
        _quad(m, P(ao, zc_a), P(bo, zc_b), P(bi2, zc_b), P(ai2, zc_a), coping, up)
        _quad(m, P(ao, zc_a - 0.12), P(bo, zc_b - 0.12), P(bo, zc_b), P(ao, zc_a), coping, o3)
        _quad(m, P(ai2, zc_a - 0.12), P(bi2, zc_b - 0.12), P(bi2, zc_b), P(ai2, zc_a), coping, -o3)
        stats["quai_wall_m"] = int(np.linalg.norm(b - a, axis=1).sum())
    stats["quai_steps"] = nst
    # Stone pines in square planters along the boulevard (OSM trees).
    rnd = np.random.default_rng(11)
    zone = terr.buffer(3.0)

    def upper(x, y):
        """On the esplanade, not at the foot of its retaining wall: a tree
        down on the quay took the upper level's height and its planter
        floated beside the wall."""
        if not zone.contains(Point(x, y)):
            return False
        if terr.buffer(-0.5).contains(Point(x, y)):
            return True
        lo = mntR.sample(np.array([x]), np.array([y]))
        return not (np.size(lo) and np.isfinite(lo[0]) and float(QA["zup"](x, y)[0]) - float(lo[0]) > 1.0)
    trunk = []
    for (x, y), t in pts:
        if t.get("natural") == "tree" and upper(x, y):
            trunk.append((x, y))
    # The row is unbroken in the street photos (2025): fill the gaps of
    # the OSM mapping at the row's own spacing, in line with the
    # neighbours.
    if len(trunk) > 2:
        T = np.array(trunk)
        order = QA["on"][QA["kd"].query(T)[1]]
        T = T[np.argsort(order)]
        blds_ = blds if blds is not None and not blds.is_empty else None
        for p0, p1 in zip(T[:-1], T[1:]):
            gap = float(np.linalg.norm(p1 - p0))
            if gap < 15 or gap > 60:
                continue
            k = int(round(gap / 10.5))
            for i in range(1, k):
                q = p0 + (p1 - p0) * i / k
                if upper(*q) and (blds_ is None or not blds_.contains(Point(*q))):
                    trunk.append((float(q[0]), float(q[1])))
    planter, soil = np.array(hexrgb("#e6dfcf")), np.array(hexrgb("#5d4a3a"))
    for x, y in trunk:
        z = float(QA["zup"](x, y)[0])
        # Square to the boulevard.
        j = QA["on"][QA["kd"].query([x, y])[1]]
        j0, j1 = max(0, j - 2), min(len(TRACK["x"]) - 1, j + 2)
        ax = np.array([TRACK["x"][j1] - TRACK["x"][j0], TRACK["y"][j1] - TRACK["y"][j0]])
        _box(m, x, y, ax, 2.3, 2.3, z - 0.3, z + 0.5, planter)
        _box(m, x, y, ax, 1.8, 1.8, z + 0.5, z + 0.52, soil)
        h = float(rnd.uniform(11.5, 15.0))
        r = float(rnd.uniform(4.2, 5.4))
        add_tree(tm, x, y, z + 0.5, h, r, "pine", rnd)
        stats["trees"] += 1
    stats["quai_pines"] = len(trunk)
    return len(trunk)


# --------------------------------------------------------------- sidewalks ----
# Around the circuit the space between the road and the buildings is
# pavement: flat paving a kerb above the road, up to the shop fronts. Left
# to the 3 m terrain grid and the aerial photo it read as sand banks and
# green smears (tree shadows, awnings) with bumps where the 2021 LiDAR saw
# Grand Prix stands, cabins and parked lorries.
SW = None
SW_REACH = 40.0                       # m from the circuit's centre line
SW_MAX_W = 14.0                       # m, widest pavement from a road's edge
SW_KERB = 0.14                        # m above the road
SW_PAVING = "#a5715f"                 # red brick pavers, as along Monaco's streets (Mapillary 2025)
SW_PALE = "#d3cbbd"                    # pale stone round the Casino (Mapillary 2025)
SW_PALE_S = (650, 1080)                # lap metres: Massenet, Casino square, Spélugues
SW_SKIP_ROAD = ("footway", "path", "steps", "cycleway", "pedestrian", "track", "bridleway", "corridor")
# From the top of Avenue d'Ostende past Casino square, Mirabeau, the Fairmont
# hairpin and Portier to the tunnel (lap metres): the street furniture as in
# the 2025 street photos (Mapillary): red-and-white painted kerbs round the
# corners, rubble-stone retaining walls, rendered garden walls with iron
# railings, low parapets with railings over the drops.
UPPER = (640, 1530)
KERB_W = 0.25                          # m, the kerb stone's top
KERB_GRANITE = "#cfccc4"
GARDEN_WALL = "#e2c690"                # ochre render (Parc Spélugues at Mirabeau)
GARDEN_RAIL = "#1d2220"                # black iron railings on the garden walls
STREET_RAIL = "#e9e9e4"                # white railings over the drops
WALL_RUBBLE = "#c6bfb0"                # pale grey limestone rubble walls
PAVER, SLAB = 9, 10                    # GROUND styles: red brick pavers, pale stone slabs
KERB, KERB_RW, RUBBLE, RAIL = 9, 10, 11, 12   # PLAIN styles (viewer/filmset.js)


def lap_s(x, y):
    """Lap metres of the nearest centre-line point."""
    _, j = TRACK["kd"].query(np.column_stack([np.atleast_1d(x), np.atleast_1d(y)]))
    return j * TRACK["step"]


def in_upper(x, y):
    s_ = lap_s(x, y)
    return (s_ >= UPPER[0]) & (s_ <= UPPER[1])


def lap_corners():
    """Lap points on a corner: the heading turns more than 18 degrees
    within 12 m either side (the red-and-white kerbs run round them)."""
    t = TRACK
    h = np.arctan2(np.gradient(t["y"]), np.gradient(t["x"]))
    k = max(1, int(round(12.0 / t["step"])))
    turn = np.abs(np.angle(np.exp(1j * (np.roll(h, -k) - np.roll(h, k)))))
    return ndimage.binary_dilation(turn > np.radians(18), iterations=k)


def _quad_uv(m, p0, p1, p2, p3, col, want, uv, kind=None, style=None):
    """_quad with texture coordinates (uv: four (k, 2) arrays, one per
    corner) and a style."""
    kind = PLAIN if kind is None else kind
    u0, u1, u2, u3 = uv
    for (a, b, c), (ua, ub, uc) in (((p0, p1, p2), (u0, u1, u2)), ((p0, p2, p3), (u0, u2, u3))):
        n = np.cross(b - a, c - a)
        flip = (n * want).sum(1) < 0
        b2 = np.where(flip[:, None], c, b)
        c2 = np.where(flip[:, None], b, c)
        ub2 = np.where(flip[:, None], uc, ub)
        uc2 = np.where(flip[:, None], ub, uc)
        col_ = np.asarray(col, np.float32)
        if col_.ndim == 1:
            col_ = np.tile(col_, (len(a), 1))
        m.tri(a, b2, c2, col_, kind, uv=np.stack([ua, ub2, uc2], 1), style=style)


def railing(m, A, B, z, col, h=1.1):
    """See-through railings from A to B (k x 2) standing on z (k,): both
    faces, the bars drawn by the shader (PLAIN style RAIL)."""
    if not len(A):
        return 0.0
    L = np.linalg.norm(B - A, axis=1)
    s0 = np.r_[0.0, np.cumsum(L)[:-1]]
    s1 = s0 + L
    u = (B - A) / np.maximum(L, 1e-6)[:, None]
    nrm = np.column_stack([u[:, 1], -u[:, 0], np.zeros(len(A))])
    z = np.asarray(z, float)
    zb = z if z.ndim else np.full(len(A), float(z))
    E = lambda p, zz: enu_fast(p[:, 0], p[:, 1], zz)
    uv = [np.column_stack([s0, np.zeros(len(A))]), np.column_stack([s1, np.zeros(len(A))]),
          np.column_stack([s1, np.ones(len(A))]), np.column_stack([s0, np.ones(len(A))])]
    for sg in (1.0, -1.0):
        _quad_uv(m, E(A, zb), E(B, zb), E(B, zb + h), E(A, zb + h), np.array(hexrgb(col)), nrm * sg, uv, style=RAIL)
    return float(L.sum())


CP_KEYS = set()                         # car park entrances (first and last point of their OSM way)
CP_LINES = []                           # their driveways, street end first
CP_HALF = 2.6                           # m, half the driveway's width


def carpark_entrances(ways, pts):
    """Short service ways from the circuit to an underground car park (OSM
    amenity=parking + parking=underground, or a parking_entrance): a
    driveway across the pavement to a garage door, not a side street."""
    if TRACK is None:
        return []
    cps = np.array([xy for xy, tg in pts if tg.get("amenity") == "parking_entrance"
                    or (tg.get("amenity") == "parking" and tg.get("parking") in ("underground", "multi-storey"))])
    if not len(cps):
        return []
    out = []
    for tg, p_, cl in ways:
        if tg.get("highway") != "service" or cl or len(p_) < 2:
            continue
        q_ = np.asarray(p_, dtype=float)
        if LineString(q_).length > 15:
            continue                    # (longer ones are ramps or streets of their own)
        ends = q_[[0, -1]]
        dcp = np.min(np.hypot(cps[:, 0][None] - ends[:, 0, None], cps[:, 1][None] - ends[:, 1, None]), axis=1)
        dtr = track_dist(ends[:, 0], ends[:, 1])
        for e in (0, 1):
            if dcp[e] < 3.0 and dtr[1 - e] < 12.0:
                CP_KEYS.add((tuple(np.round(q_[0], 2)), tuple(np.round(q_[-1], 2))))
                out.append(q_ if e == 1 else q_[::-1])        # from the street to the door
                break
    CP_LINES[:] = out
    return out


def garage_doors(m, entrances, fps, stats):
    """Each car park entrance's door in the facade where its driveway
    meets the building: a dark opening 3.6 m wide and 2.5 m high with a
    stone lintel, the pavement running on across it."""
    n_ = 0
    blds = unary_union(fps) if fps else None
    for q_ in entrances:
        if blds is None:
            break
        end, back = q_[-1], q_[0]
        u = back - end
        u = u / (np.linalg.norm(u) + 1e-9)
        # The door: across the driveway's end (where the OSM way meets the
        # car park), in the wall that holds back the slope above it; or
        # the facade, if a building comes first.
        ray = LineString([back, end])
        hit = ray.intersection(blds.boundary)
        hp = end
        if not hit.is_empty:
            hp = min([np.asarray(g.coords)[0] for g in getattr(hit, "geoms", [hit])],
                     key=lambda c: np.hypot(*(c - back)))
        v = np.array([-u[1], u[0]])
        z0 = float(SW["zw"](hp[0], hp[1])[0]) if SW is not None else float(TRACK["z"][TRACK["kd"].query(hp)[1]])
        E = lambda p, z: enu_fast(p[:, 0], p[:, 1], np.array([z]))
        o3 = np.array([[u[0], u[1], 0.0]])
        # A stone surround the width of the driveway, in case the wall
        # behind is lower than the door.
        s0, s1 = (hp - v * CP_HALF + u * 0.02)[None], (hp + v * CP_HALF + u * 0.02)[None]
        _quad(m, E(s0, z0 - 0.3), E(s1, z0 - 0.3), E(s1, z0 + 3.3), E(s0, z0 + 3.3), np.array(hexrgb(SW_WALL))[None], o3)
        c = hp + u * 0.06
        a, b = c - v * 1.8, c + v * 1.8
        A, B = a[None], b[None]
        _quad(m, E(A, z0 - 0.05), E(B, z0 - 0.05), E(B, z0 + 2.5), E(A, z0 + 2.5), np.array(hexrgb("#1f2023"))[None], o3)
        A2, B2 = (a - v * 0.25 + u * 0.02)[None], (b + v * 0.25 + u * 0.02)[None]
        _quad(m, E(A2, z0 + 2.5), E(B2, z0 + 2.5), E(B2, z0 + 2.85), E(A2, z0 + 2.85), np.array(hexrgb("#e2dbcb"))[None], o3)
        n_ += 1
    stats["carpark_doors"] = n_
    return n_


def is_carpark_way(p_):
    q_ = np.asarray(p_, dtype=float)
    return (tuple(np.round(q_[0], 2)), tuple(np.round(q_[-1], 2))) in CP_KEYS


def _road_polys(ways):
    """OSM carriageways at their mapped width (as road_surfaces() draws
    them) with heights sampled along their centre lines."""
    polys, P = [], []
    for t, p_, closed in ways:
        hw = t.get("highway")
        if hw not in ROAD_W or t.get("tunnel") in ("yes", "building_passage") or t.get("area") == "yes" \
                or t.get("layer", "0") not in ("0",) or t.get("bridge") or len(p_) < 2 or is_carpark_way(p_):
            continue
        W = float(ROAD_W[hw])
        try:
            if t.get("width"):
                W = float(t["width"].replace("m", "").strip())
            elif t.get("lanes"):
                W = max(W, 3.1 * float(t["lanes"]))
        except ValueError:
            pass
        W = float(np.clip(W, 3.0, 20.0))
        ln = LineString(p_)
        polys.append(ln.buffer(W / 2, cap_style=2, join_style=2))
        for s in np.arange(0, ln.length + 0.01, 2.0):
            q = ln.interpolate(s)
            P.append((q.x, q.y))
    return polys, np.array(P) if P else np.zeros((0, 2))


def sidewalks_setup(ways, mnt, sea, feats):
    """Works out where the pavements are along the circuit and sets the
    terrain model there to kerb height above the nearest road."""
    if TRACK is None:
        return None
    t = TRACK
    corridor = t["line"].buffer(SW_REACH)
    rp, rpts = _road_polys(ways)
    track_poly = t["line"].buffer(TRACK_HALF + 0.35)
    roads = unary_union([q for q in rp if q.intersects(corridor)] + [track_poly])
    # Road heights: on and near the circuit the lap's own (the modelled
    # road); elsewhere the LiDAR ground along the road's centre line.
    sel = contains_xy(corridor.buffer(SW_MAX_W + 5), rpts[:, 0], rpts[:, 1]) if len(rpts) else np.zeros(0, bool)
    rpts = rpts[sel]
    zr = Raster(ndimage.median_filter(np.nan_to_num(mnt, nan=0.0), 5)).sample(rpts[:, 0], rpts[:, 1])
    dk, jk = t["kd"].query(rpts)
    zr = np.where(dk < TRACK_HALF + 4.0, t["z"][jk], zr)
    ok = np.isfinite(zr)
    rx = np.r_[rpts[ok, 0], t["x"]]
    ry = np.r_[rpts[ok, 1], t["y"]]
    rz = np.r_[zr[ok], t["z"]]
    from scipy.spatial import cKDTree
    rkd = cKDTree(np.column_stack([rx, ry]))

    def zroad(x, y):
        _, j = rkd.query(np.column_stack([np.atleast_1d(x), np.atleast_1d(y)]))
        return rz[j]

    # What is not pavement: buildings, water, gardens, car parks, other
    # people's places (Quai Albert 1er, Mareterra, the Ostende promenade,
    # the tunnel).
    blds = unary_union([shapely.force_2d(shape(f["geometry"])).buffer(0) for f in feats
                        if shape(f["geometry"]).intersects(corridor)])
    GREENK = ("grass", "park", "garden", "wood", "forest", "meadow", "scrub", "orchard", "playground", "pitch",
              "flowerbed", "village_green")
    other = []
    for tg, p_, closed in ways:
        if not closed or len(p_) < 4:
            continue
        k = tg.get("leisure") or tg.get("landuse") or tg.get("natural")
        if k in GREENK or tg.get("amenity") == "parking" or tg.get("leisure") in ("swimming_pool", "sports_centre") \
                or tg.get("amenity") == "fountain" or (tg.get("highway") and tg.get("area") == "yes"
                                                       and tg.get("highway") != "pedestrian"):
            g = Polygon(p_).buffer(0)
            if g.intersects(corridor):
                other.append(g)
    if QA is not None:
        other.append(QA["poly"].buffer(0.3))
    if MT is not None:
        other.append(MT["zone"])
    if TUNNEL is not None:
        other.append(TUNNEL["ground"].buffer(6))
        other.append(TUNNEL["approach"])
        if "open_ground" in TUNNEL:
            other.append(TUNNEL["open_ground"])
    if PROM is not None:
        i = PROM["idx"]
        a = np.column_stack([t["x"][i], t["y"][i]])
        b = a + np.column_stack([PROM["nx"], PROM["ny"]]) * (PROM["pd"][:, None] + 1.5)
        prom_poly = Polygon(np.r_[a, b[::-1]]).buffer(0)
        other.append(prom_poly)
    sea_poly = cells_to_poly(ndimage.zoom(sea.astype(np.uint8), 0.25, order=0) > 0, X0, Y1, RES * 4) if sea.any() else Polygon()
    walk = corridor.intersection(roads.buffer(SW_MAX_W)).difference(roads)
    walk = walk.difference(blds).difference(unary_union(other)).difference(sea_poly.buffer(4.5))   # quay_walls() lays the 4 m by the water
    # Only where the ground is near the road's level: a cliff, a garden
    # wall or a lower quay beside the road stays as it is.
    mk = raster_mask(walk)
    rr, cc = np.nonzero(mk)
    xs, ys = cell_xy(rr, cc)
    zw = zroad(xs, ys) + SW_KERB
    g = mnt[rr, cc]
    level = np.isfinite(g) & (np.abs(g - zw) < 1.2)
    lv = np.zeros_like(mk)
    lv[rr[level], cc[level]] = True
    # Patches of LiDAR humps (stands, vans) count as level if surrounded.
    lv = ndimage.binary_closing(lv, iterations=4) & mk
    lv = ndimage.binary_opening(lv, iterations=1)       # (narrow pavements, 1.5 m, stay)
    # Only pieces that touch a road.
    lab, n = ndimage.label(lv)
    rmask = raster_mask(roads.buffer(1.0))
    touch = np.unique(lab[lv & rmask])
    lv &= np.isin(lab, touch[touch > 0])
    k = 2                                         # polygons from a 1 m grid
    h_, w_ = lv.shape[0] // k * k, lv.shape[1] // k * k
    lv1 = lv[:h_, :w_].reshape(h_ // k, k, w_ // k, k).mean(axis=(1, 3)) >= 0.5
    walk_lv = cells_to_poly(lv1, X0, Y1, RES * k)
    # Back to the exact road edges and building faces where it meets them.
    walk = walk_lv.buffer(0.8, join_style=2).intersection(walk)
    # Along the circuit itself, a continuous strip each side from the
    # road's edge to the first building (a little under it), side street,
    # garden or change of level, whatever the raster test above found.
    stop = [sea_poly.buffer(1.0), unary_union(other)]
    lap_w = []
    for tg, p_, cl in ways:
        hw = tg.get("highway")
        if hw not in ROAD_W or len(p_) < 2 or tg.get("tunnel") or tg.get("bridge") or is_carpark_way(p_):
            continue
        q_ = np.asarray(p_, dtype=float)
        # Segment by segment (a way can follow the lap, then turn off it).
        L_ = np.hypot(*np.diff(q_, axis=0).T)
        k_ = np.maximum(1, np.ceil(L_ / 2.0)).astype(int)
        dense = np.vstack([q_[i] + (q_[i + 1] - q_[i]) * np.linspace(0, 1, k_[i] + 1)[:-1, None]
                           for i in range(len(q_) - 1)] + [q_[-1:]])
        off_lap = track_dist(dense[:, 0], dense[:, 1]) > 4.0
        lab_, nl_ = ndimage.label(off_lap)
        for li in range(1, nl_ + 1):
            ii = np.nonzero(lab_ == li)[0]
            seg_ = dense[max(ii[0] - 1, 0):ii[-1] + 2]
            if len(seg_) > 1:
                stop.append(LineString(seg_).buffer(float(ROAD_W[hw]) / 2, cap_style=2))
    strip = track_strip(blds, unary_union(stop), mnt)
    # Car park driveways: paved at the street's level up to the door, the
    # slope above the car park held back by walls each side and over the
    # door (edge_walls() finds them), not a ramp of ground.
    drives = []
    for q_ in CP_LINES:
        cor = LineString(q_).buffer(CP_HALF, cap_style=2)
        # A building a few metres to one side: the pavement runs on to its
        # facade (a ramp of ground in the gap stood up as a red wedge).
        reach = LineString(q_).buffer(CP_HALF + 5.0, cap_style=2)
        osm_b = [Polygon(p_).buffer(0) for tg, p_, cl in ways if cl and len(p_) > 3 and tg.get("building")
                 and Polygon(p_).buffer(0).intersects(reach)]
        nb = unary_union([blds.intersection(reach)] + [g_.intersection(reach) for g_ in osm_b])
        if not nb.is_empty:
            cor = unary_union([cor, nb]).convex_hull.intersection(reach)
        drives.append(cor.difference(blds).difference(unary_union(osm_b) if osm_b else Polygon())
                      .difference(t["line"].buffer(SW_START)))
    walk = unary_union([walk, strip] + drives)
    # (Level with the lap a few metres either side too, to the facade.)
    drives_u = unary_union([LineString(q_).buffer(CP_HALF + 7.0) for q_ in CP_LINES]) if drives else None
    # The Fairmont hairpin's island, between the way in and the way out
    # (2.5 m lower by the time they part): paved at the way in's level, a
    # wall down to the way out.
    hp = hairpin_island(blds)
    if hp is not None:
        walk = unary_union([walk.difference(hp["poly"].buffer(1.0)), hp["poly"]])
        hp_zone = hp["poly"].buffer(0.5)       # (its edge points too)
    walk = unary_union([q for q in to_polys(walk) if q.area > 6])
    if walk.is_empty:
        return None
    def zfn(x, y):
        """Kerb height: beside the circuit its own road's (a parallel street
        up or down the slope must not tilt the pavement), elsewhere the
        nearest street's."""
        x, y = np.atleast_1d(x), np.atleast_1d(y)
        dk_, jk_ = t["kd"].query(np.column_stack([x, y]))
        lap = dk_ < SW_START + SW_STRIP_W + 1.0
        if drives_u is not None:
            # (A driveway stays at the lap's level to its door: the street
            # on the terrace above the car park had tilted it into a wedge.)
            lap |= contains_xy(drives_u, x, y)
        z_ = np.where(lap, t["z"][jk_], zroad(x, y))
        if hp is not None:
            z_ = np.where(contains_xy(hp_zone, x, y), hp["z"](x, y), z_)
        return z_ + SW_KERB
    skip = [blds.buffer(0.5), sea_poly.buffer(1.0)]
    if QA is not None:
        skip.append(QA["poly"].buffer(1.0))
    if MT is not None:
        skip.append(MT["zone"])
    if TUNNEL is not None:
        skip += [TUNNEL["ground"].buffer(6), TUNNEL["approach"]]
    if PROM is not None:
        skip.append(prom_poly.buffer(1.0))
    steps = [LineString(p_).buffer(2.0) for tg, p_, cl in ways
             if tg.get("highway") == "steps" and len(p_) > 1 and LineString(p_).intersects(corridor)]
    others = unary_union([q for q in rp if q.intersects(corridor)]).difference(track_poly.buffer(0.5))
    kerbs = side_street_steps(walk, others, mnt, zfn)
    gardens = unary_union([Polygon(p_).buffer(0) for tg, p_, cl in ways if cl and len(p_) > 3
                           and (tg.get("leisure") or tg.get("landuse") or tg.get("natural")) in
                           ("park", "garden", "grass", "flowerbed", "village_green")
                           and Polygon(p_).buffer(0).intersects(corridor)])
    walls = edge_walls(unary_union([walk, roads.intersection(corridor)]), mnt, zfn,
                       unary_union(skip + steps), gardens)
    mk = raster_mask(walk)
    rr, cc = np.nonzero(mk)
    xs, ys = cell_xy(rr, cc)
    mnt[rr, cc] = zfn(xs, ys)
    print(f"sidewalks: {walk.area:.0f} m2 along the circuit, walls {walls['up_m']:.0f} m up, {walls['down_m']:.0f} m down")
    # Red brick pavers almost everywhere; pale stone round the Casino.
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    i0, i1 = int(SW_PALE_S[0] / step), int(SW_PALE_S[1] / step)
    pale_zone = LineString(np.column_stack([t["x"][i0:i1], t["y"][i0:i1]])).buffer(SW_REACH + SW_MAX_W)
    # The corners where a side street comes into the circuit (the paved
    # islands between the two): paved like the pavements, at their own
    # height, not left as the photo's dark smudge.
    isl = []
    for tg, p_, cl in ways:
        hw = tg.get("highway")
        if hw not in ROAD_W or cl or len(p_) < 2 or tg.get("tunnel") or tg.get("bridge") or is_carpark_way(p_):
            continue
        q_ = np.asarray(p_, dtype=float)
        if LineString(q_).length < 20:
            continue
        de = track_dist(q_[[0, -1], 0], q_[[0, -1], 1])
        for e in (0, -1):
            if de[e if e == 0 else 1] < 1.5 and track_dist(q_[[1 if e == 0 else -2], 0], q_[[1 if e == 0 else -2], 1])[0] > 3.0:
                isl.append(Point(*q_[e]).buffer(12.0))
    islands = Polygon()
    if isl:
        islands = unary_union(isl).intersection(t["line"].buffer(SW_START + SW_STRIP_W + 2.0))
        islands = islands.difference(roads).difference(walk.buffer(-0.05)).difference(blds).difference(unary_union(skip + other))
        islands = unary_union([q for q in to_polys(islands) if q.area > 4 and q.buffer(0.3).intersects(walk)])
        print(f"side street corners paved: {islands.area:.0f} m2")
    return {"poly": walk, "pale": walk.intersection(pale_zone), "zw": zfn, "walls": walls, "steps": kerbs,
            "islands": islands, "roads": roads.buffer(0.3)}


def side_street_steps(walk, others, mnt, zfn):
    """Where a pavement of the circuit meets a side street that runs above
    it (Avenue de la Costa coming down beside Avenue d'Ostende), the step
    between them: without it the 3 m terrain grid smeared a dark slope over
    the side street's edge. Segments of the pavement's edge whose outside
    is that street, with the street's ground (LiDAR, beside the edge) as
    the top."""
    R = Raster(mnt)
    A, B, O, ZW, TOP = [], [], [], [], []
    for pg in to_polys(walk):
        for ring in [pg.exterior] + list(pg.interiors):
            L = ring.length
            if L < 3:
                continue
            ss = np.linspace(0, L, max(3, int(L / 1.0)) + 1)
            q = shapely.line_interpolate_point(ring, ss)
            P = np.column_stack([shapely.get_x(q), shapely.get_y(q)])
            a, b = P[:-1], P[1:]
            mid = (a + b) / 2
            u = b - a
            ln = np.linalg.norm(u, axis=1)
            ok = ln > 0.05
            a, b, mid, u, ln = a[ok], b[ok], mid[ok], u[ok], ln[ok]
            u = u / ln[:, None]
            out = np.column_stack([u[:, 1], -u[:, 0]])
            t_ = mid + out * 0.3
            out[contains_xy(walk, t_[:, 0], t_[:, 1])] *= -1
            o1 = mid + out * 0.6
            on = contains_xy(others, o1[:, 0], o1[:, 1])
            if not on.any():
                continue
            zw = zfn(mid[:, 0], mid[:, 1])
            sm = np.stack([R.sample(mid[:, 0] + out[:, 0] * d, mid[:, 1] + out[:, 1] * d) for d in (1.0, 1.5, 2.0)])
            fin = np.isfinite(sm).all(0)
            top = np.where(fin, np.median(np.where(fin, sm, 0), 0), zw)
            up = on & fin & (top - zw > 0.45) & (top - zw < 6.0)
            up = ndimage.binary_opening(ndimage.binary_closing(up, iterations=1), iterations=1)
            top = ndimage.median_filter(top, 5, mode="nearest")
            A.append(a[up]); B.append(b[up]); O.append(out[up]); ZW.append(zw[up]); TOP.append(top[up] + 0.05)
    if not A:
        return None
    w = {k_: np.concatenate(v) for k_, v in (("a", A), ("b", B), ("out", O), ("zw", ZW), ("top", TOP))}
    w["m"] = float(np.linalg.norm(w["b"] - w["a"], axis=1).sum())
    print(f"side street steps: {w['m']:.0f} m")
    return w


SW_WALL = "#d9ccb1"                    # Monaco's retaining walls: warm stone and render
SW_WALL_REACH = 25.0                   # m from the circuit's centre line
SW_CAP = 4.5                           # m of level ground drawn on top of a wall


def edge_walls(U, mnt, zfn, skip, gardens=None):
    """Round the edge of the street (circuit, roads and pavements), metre by
    metre: where the ground beside it is more than a metre higher, a
    retaining wall up to it; where it drops more than 1.2 m, a wall down
    to it with a stone parapet. Left to the 3 m terrain grid these were
    lumps of smeared photo rising from the kerb and slopes falling off it."""
    R = Raster(mnt)
    # Traffic islands and medians (small holes in the street) keep their
    # kerbs: no walls round them.
    U = unary_union([Polygon(pg.exterior, [h for h in pg.interiors if Polygon(h).area > 300])
                     for pg in to_polys(U)])
    A, B, OUT, KIND, TOP, LOW, ZW, GARD = [], [], [], [], [], [], [], []
    for pg in to_polys(U):
        for ring in [pg.exterior] + list(pg.interiors):
            L = ring.length
            if L < 4:
                continue
            ss = np.linspace(0, L, max(4, int(L / 1.0)) + 1)
            q = shapely.line_interpolate_point(ring, ss)
            P = np.column_stack([shapely.get_x(q), shapely.get_y(q)])
            a, b = P[:-1], P[1:]
            mid = (a + b) / 2
            u = b - a
            ln = np.linalg.norm(u, axis=1)
            ok = ln > 0.05
            a, b, mid, u, ln = a[ok], b[ok], mid[ok], u[ok], ln[ok]
            u = u / ln[:, None]
            out = np.column_stack([u[:, 1], -u[:, 0]])
            t_ = mid + out * 0.3
            flip = contains_xy(U, t_[:, 0], t_[:, 1])
            out[flip] *= -1
            near = track_dist(mid[:, 0], mid[:, 1]) < SW_WALL_REACH
            o1 = mid + out * 1.0
            near &= ~contains_xy(skip, o1[:, 0], o1[:, 1])
            # Never a wall, or the ground on top of one, on the circuit (a
            # side road's edge facing another leg of the lap).
            for d_ in (1.0, 3.0, SW_CAP + 0.5):
                px_, py_ = mid[:, 0] + out[:, 0] * d_, mid[:, 1] + out[:, 1] * d_
                near &= track_dist(px_, py_) > TRACK_HALF + 1.0
                near &= ~contains_xy(U, px_, py_)       # nor across a narrow gap on to another street
            if not near.any():
                continue
            zw = zfn(mid[:, 0], mid[:, 1])
            sm = np.stack([R.sample(mid[:, 0] + out[:, 0] * d, mid[:, 1] + out[:, 1] * d) for d in (1.0, 2.0, 3.0)])
            fin = np.isfinite(sm).all(0)
            top = np.where(fin, np.median(np.where(fin, sm, 0), 0), zw)
            low = np.where(fin, np.min(np.where(fin, sm, 0), 0), zw)
            up = near & fin & (top - zw > 0.7)
            down = near & fin & ~up & (low < zw - 1.2)
            # No single-metre walls or gaps.
            up = ndimage.binary_opening(ndimage.binary_closing(up, iterations=1), iterations=1)
            down = ndimage.binary_opening(ndimage.binary_closing(down, iterations=1), iterations=1) & ~up
            top = ndimage.median_filter(np.minimum(top, zw + 12.0), 5, mode="nearest")
            low = ndimage.minimum_filter1d(np.maximum(low, zw - 10.0), 3, mode="nearest")
            # Gardens beside the circuit from Ostende to the tunnel: a
            # rendered wall with an iron railing all along, low (60 cm)
            # where the garden is level with the pavement.
            gard = np.zeros(len(mid), bool)
            if gardens is not None and not gardens.is_empty:
                gard = near & contains_xy(gardens, o1[:, 0], o1[:, 1]) & in_upper(mid[:, 0], mid[:, 1])
                gard = ndimage.binary_opening(ndimage.binary_closing(gard, iterations=1), iterations=1) & ~down
                low_g = gard & ~up
                top = np.where(low_g, zw + 0.6, top)
                up = up | gard
            k = up | down
            A.append(a[k]); B.append(b[k]); OUT.append(out[k]); KIND.append(up[k]); GARD.append(gard[k])
            TOP.append(np.maximum(top[k], zw[k] + 0.6)); LOW.append(low[k]); ZW.append(zw[k])
    if not A:
        return {"up_m": 0.0, "down_m": 0.0, "a": np.zeros((0, 2))}
    w = {k_: np.concatenate(v) for k_, v in (("a", A), ("b", B), ("out", OUT), ("up", KIND), ("top", TOP),
                                             ("low", LOW), ("zw", ZW), ("garden", GARD))}
    seg = np.linalg.norm(w["b"] - w["a"], axis=1)
    w["up_m"], w["down_m"] = float(seg[w["up"]].sum()), float(seg[~w["up"]].sum())
    return w


def wall_ground(X, Y, Zm):
    """Ground nodes just behind a retaining wall go down to the street, so
    the 3 m grid's slope stays behind the wall (under its cap) instead of
    rising in front of it."""
    w = SW["walls"] if SW is not None else None
    if w is None or not len(w["a"]) or not w["up"].any():
        return Zm
    from scipy.spatial import cKDTree
    m_ = (w["a"][w["up"]] + w["b"][w["up"]]) / 2
    o_ = w["out"][w["up"]]
    zw = w["zw"][w["up"]]
    d, j = cKDTree(m_).query(np.column_stack([X.ravel(), Y.ravel()]), distance_upper_bound=GRID + 0.6)
    hit = np.isfinite(d)
    jj = np.where(hit, j, 0)
    behind = ((X.ravel() - m_[jj, 0]) * o_[jj, 0] + (Y.ravel() - m_[jj, 1]) * o_[jj, 1]) > -0.2
    sel = (hit & behind).reshape(X.shape)
    if sel.any():
        Zm = Zm.copy()
        Zm[sel] = np.minimum(Zm[sel], zw[jj.reshape(X.shape)[sel]] - 0.3)
    return Zm


def street_walls(gm, m, stats):
    """The walls edge_walls() found: stone faces, a coping, level ground on
    top of the retaining walls and a parapet along the drops."""
    w = SW["walls"] if SW is not None else None
    if w is None or not len(w["a"]):
        return 0
    rng = np.random.default_rng(11)
    n = len(w["a"])
    tint = (0.93 + 0.09 * rng.random(n))[:, None]
    stone = np.array(hexrgb(SW_WALL))[None] * tint
    cope = np.array(hexrgb("#ece4d3"))
    a, b, o, zw, top, low = w["a"], w["b"], w["out"], w["zw"], w["top"], w["low"]
    o3 = np.column_stack([o, np.zeros(n)])

    def E(p, z):
        return enu_fast(p[:, 0], p[:, 1], z)
    up = w["up"]
    gard = w.get("garden", np.zeros(n, bool))
    upper = in_upper((a[:, 0] + b[:, 0]) / 2, (a[:, 1] + b[:, 1]) / 2)
    seg = np.linalg.norm(b - a, axis=1)
    s0 = np.r_[0.0, np.cumsum(seg)[:-1]]
    s1 = s0 + seg
    rails = 0.0
    if up.any():
        A, B, O, Z0, Z1 = a[up], b[up], o3[up], zw[up] - 0.4, top[up]
        # Faces: rendered garden walls, rubble stone from Ostende to the
        # tunnel (as along Mirabeau), the old dressed stone elsewhere.
        g_, r_ = gard[up], upper[up] & ~gard[up]
        p_ = ~(g_ | r_)
        if p_.any():
            _quad(m, E(A[p_], Z0[p_]), E(B[p_], Z0[p_]), E(B[p_], Z1[p_]), E(A[p_], Z1[p_]), stone[up][p_], -O[p_])
        if g_.any():
            gc = np.array(hexrgb(GARDEN_WALL))[None] * tint[up][g_]
            _quad(m, E(A[g_], Z0[g_]), E(B[g_], Z0[g_]), E(B[g_], Z1[g_]), E(A[g_], Z1[g_]), gc, -O[g_])
            rails += railing(m, A[g_] + O[g_, :2] * 0.15, B[g_] + O[g_, :2] * 0.15, Z1[g_] + 0.06, GARDEN_RAIL, 1.2)
        if r_.any():
            S0, S1 = s0[up][r_], s1[up][r_]
            rc = np.array(hexrgb(WALL_RUBBLE))[None] * tint[up][r_]
            _quad_uv(m, E(A[r_], Z0[r_]), E(B[r_], Z0[r_]), E(B[r_], Z1[r_]), E(A[r_], Z1[r_]), rc, -O[r_],
                     [np.column_stack([S0, np.zeros(r_.sum())]), np.column_stack([S1, np.zeros(r_.sum())]),
                      np.column_stack([S1, Z1[r_] - Z0[r_]]), np.column_stack([S0, Z1[r_] - Z0[r_]])], style=RUBBLE)
            # (A railing along the top of the taller ones.)
            hi_ = (Z1[r_] - Z0[r_]) > 2.4
            if hi_.any():
                rails += railing(m, (A[r_] + O[r_, :2] * 0.15)[hi_], (B[r_] + O[r_, :2] * 0.15)[hi_],
                                 Z1[r_][hi_] + 0.06, GARDEN_RAIL, 1.0)
        # Coping along the top, and the ground on top of the wall.
        c0a, c0b = A - O[:, :2] * 0.05, B - O[:, :2] * 0.05
        c1a, c1b = A + O[:, :2] * 0.35, B + O[:, :2] * 0.35
        upz = np.tile([0.0, 0.0, 1.0], (up.sum(), 1))
        _quad(m, E(c0a, Z1 + 0.06), E(c0b, Z1 + 0.06), E(c1b, Z1 + 0.06), E(c1a, Z1 + 0.06), cope, upz)
        _quad(m, E(c0a, Z1 - 0.1), E(c0b, Z1 - 0.1), E(c0b, Z1 + 0.06), E(c0a, Z1 + 0.06), cope * 0.9, -O)
        ca, cb = A + O[:, :2] * SW_CAP, B + O[:, :2] * SW_CAP
        V = np.concatenate([c1a, c1b, cb, ca])
        Z = np.concatenate([Z1, Z1, Z1, Z1]) + 0.04
        k = up.sum()
        idx = np.concatenate([np.column_stack([np.arange(k), np.arange(k) + k, np.arange(k) + 2 * k]),
                              np.column_stack([np.arange(k), np.arange(k) + 2 * k, np.arange(k) + 3 * k])])
        uv = np.column_stack([(V[:, 0] - X0) / (X1 - X0), (Y1 - V[:, 1]) / (Y1 - Y0)])
        P = enu_fast(V[:, 0], V[:, 1], Z)
        # Wound to face up (the ring runs either way round).
        e1, e2 = P[idx[:, 1]] - P[idx[:, 0]], P[idx[:, 2]] - P[idx[:, 0]]
        fl = np.cross(e1, e2)[:, 2] < 0
        idx[fl] = idx[fl][:, [0, 2, 1]]
        gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=idx)
    dn = ~up
    if dn.any():
        A, B, O = a[dn], b[dn], o3[dn]
        # Over the drops a parapet: from Ostende to the tunnel a low one
        # (45 cm) with a white railing on it, as in the street photos.
        ud = upper[dn]
        Z0, Zt = low[dn] - 0.3, zw[dn] + np.where(ud, 0.45, 0.9)
        tall = (zw[dn] - low[dn]) > 1.5
        Zt = np.where(tall, Zt, zw[dn])
        rr = tall & ud
        if rr.any():
            rails += railing(m, (A + O[:, :2] * 0.15)[rr], (B + O[:, :2] * 0.15)[rr], Zt[rr] + 0.06, STREET_RAIL, 0.75)
        Ao, Bo = A + O[:, :2] * 0.3, B + O[:, :2] * 0.3
        _quad(m, E(Ao, Z0), E(Bo, Z0), E(Bo, Zt), E(Ao, Zt), stone[dn] * 0.95, O)
        if tall.any():
            t_ = tall
            _quad(m, E(A[t_], zw[dn][t_] - 0.2), E(B[t_], zw[dn][t_] - 0.2), E(B[t_], Zt[t_]), E(A[t_], Zt[t_]),
                  stone[dn][t_], -O[t_])
            upz = np.tile([0.0, 0.0, 1.0], (t_.sum(), 1))
            ci, cb_ = A[t_] - O[t_, :2] * 0.04, B[t_] - O[t_, :2] * 0.04
            co, cob = Ao[t_] + O[t_, :2] * 0.04, Bo[t_] + O[t_, :2] * 0.04
            _quad(m, E(ci, Zt[t_] + 0.06), E(cb_, Zt[t_] + 0.06), E(cob, Zt[t_] + 0.06), E(co, Zt[t_] + 0.06), cope, upz)
    k_ = SW.get("steps")
    if k_ is not None and len(k_["a"]):
        A, B = k_["a"], k_["b"]
        O = np.column_stack([k_["out"], np.zeros(len(A))])
        nk = len(A)
        st = np.array(hexrgb(SW_WALL))[None] * (0.93 + 0.09 * rng.random(nk))[:, None]
        _quad(m, E(A, k_["zw"] - 0.4), E(B, k_["zw"] - 0.4), E(B, k_["top"]), E(A, k_["top"]), st, -O)
        ca, cb = A - O[:, :2] * 0.05, B - O[:, :2] * 0.05
        c1a, c1b = A + O[:, :2] * 0.3, B + O[:, :2] * 0.3
        upz = np.tile([0.0, 0.0, 1.0], (nk, 1))
        _quad(m, E(ca, k_["top"] + 0.04), E(cb, k_["top"] + 0.04), E(c1b, k_["top"] + 0.04), E(c1a, k_["top"] + 0.04),
              cope, upz)
        stats["side_street_steps_m"] = int(k_["m"])
    stats["street_railing_m"] = int(rails)
    stats["garden_wall_m"] = int(seg[gard].sum())
    print(f"railings: {rails:.0f} m, garden walls {seg[gard].sum():.0f} m")
    stats["street_wall_up_m"] = int(w["up_m"])
    stats["street_wall_down_m"] = int(w["down_m"])
    return n


SW_START = TRACK_HALF + 0.1            # the pavement's kerb, just past the circuit's edge line
SW_STRIP_W = 8.0                       # m, the widest strip along the lap (wider places come from the raster test)


def track_strip(blds, stop, mnt):
    """Pavement both sides of the circuit, 2 m by 2 m along the lap: from
    the kerb out to whatever comes first, a building (the strip runs half
    a metre under it), a side street, a garden or a quay (stop), or the
    ground more than a metre off the kerb's level (a wall: edge_walls()
    builds it); at most SW_MAX_W wide."""
    t = TRACK
    x, y, z = t["x"], t["y"], t["z"]
    n = len(x)
    dx = np.gradient(np.r_[x[-1], x, x[0]])[1:-1]
    dy = np.gradient(np.r_[y[-1], y, y[0]])[1:-1]
    ln = np.hypot(dx, dy) + 1e-9
    lx, ly = -dy / ln, dx / ln
    ds = SW_START + np.arange(0.0, SW_STRIP_W, 0.25)
    R = Raster(mnt)
    quads = []
    for side in (1.0, -1.0):
        PX = x[:, None] + side * lx[:, None] * ds[None]
        PY = y[:, None] + side * ly[:, None] * ds[None]
        fx, fy = PX.ravel(), PY.ravel()
        inb = contains_xy(blds, fx, fy).reshape(PX.shape)
        ins = contains_xy(stop, fx, fy).reshape(PX.shape)
        g = R.sample(fx, fy).reshape(PX.shape)
        off = ~np.isfinite(g) | (np.abs(g - (z[:, None] + SW_KERB)) > 1.0)
        # (Another leg of the lap is a stop too.)
        oth = (track_dist(fx, fy).reshape(PX.shape) < ds[None] - 1.0)
        end = inb | ins | off | oth
        first = np.where(end.any(1), end.argmax(1), len(ds))
        w = ds[np.minimum(first, len(ds) - 1)] - SW_START
        w = np.where(first >= len(ds), SW_STRIP_W, w)
        hitb = (first < len(ds)) & inb[np.arange(n), np.minimum(first, len(ds) - 1)]
        w = np.where(hitb, w + 0.5, w)
        w = ndimage.median_filter(w, 3, mode="wrap")
        for i in range(n):
            j = (i + 1) % n
            if w[i] < 0.6 or w[j] < 0.6:
                continue
            a0 = (x[i] + side * lx[i] * SW_START, y[i] + side * ly[i] * SW_START)
            b0 = (x[j] + side * lx[j] * SW_START, y[j] + side * ly[j] * SW_START)
            b1 = (x[j] + side * lx[j] * (SW_START + w[j]), y[j] + side * ly[j] * (SW_START + w[j]))
            a1 = (x[i] + side * lx[i] * (SW_START + w[i]), y[i] + side * ly[i] * (SW_START + w[i]))
            q = Polygon([a0, b0, b1, a1]).buffer(0.05, join_style=2)
            if q.is_valid and not q.is_empty:
                quads.append(q)
    strip = unary_union(quads).difference(t["line"].buffer(SW_START))
    if TUNNEL is not None:
        strip = strip.difference(TUNNEL["ground"].buffer(6)).difference(TUNNEL["approach"])
    strip = strip.difference(blds.buffer(-0.5) if not blds.is_empty else blds)
    return unary_union([q for q in to_polys(strip) if q.area > 4])


HAIRPIN = (1190.0, 1265.0, 1345.0)     # lap metres: the Fairmont hairpin's way in, apex, way out


def hairpin_island(blds):
    """The paved island inside the Fairmont hairpin, between its two legs,
    up to 45 m back from the apex (buildings cut out), with its level: the
    way in's (the higher leg)."""
    if TRACK is None:
        return None
    t = TRACK
    st = t["step"]
    idx = np.arange(int(HAIRPIN[0] / st), int(HAIRPIN[2] / st))
    loop = Polygon(np.column_stack([t["x"][idx], t["y"][idx]])).buffer(0)
    ia = int(HAIRPIN[1] / st)
    apex = Point(t["x"][ia], t["y"][ia])
    isl = loop.difference(t["line"].buffer(SW_START)).intersection(apex.buffer(45.0))
    if not blds.is_empty:
        isl = isl.difference(blds)
    parts = [q for q in to_polys(isl) if q.area > 8]
    if not parts:
        return None
    isl = max(parts, key=lambda q: q.area)
    from scipy.spatial import cKDTree
    i_in = np.arange(int(HAIRPIN[0] / st), ia + 1)
    kd_in = cKDTree(np.column_stack([t["x"][i_in], t["y"][i_in]]))
    z_in = t["z"][i_in]

    def z(x, y):
        _, j = kd_in.query(np.column_stack([np.atleast_1d(x), np.atleast_1d(y)]))
        return z_in[j]
    print(f"Fairmont hairpin island: {isl.area:.0f} m2")
    return {"poly": isl, "z": z}


def sidewalk_ground(X, Y, Zm, Zs):
    """Ground nodes under the pavement slab: just below it."""
    if SW is None:
        return Zm, Zs
    ins = contains_xy(SW["poly"], X, Y)
    if ins.any():
        zw = SW["zw"](X[ins], Y[ins])
        Zm = Zm.copy(); Zs = Zs.copy()
        # Only ever lowered (the track's own rules keep the ground under
        # the road and the barriers).
        Zm[ins] = np.minimum(Zm[ins], zw - 0.16)
        Zs[ins] = zw
    return wall_ground(X, Y, Zm), Zs


def sidewalks(gm, m, stats):
    """The pavement slab, laid by the shader (red brick pavers, pale stone
    round the Casino) and shaded from the ground map, with kerb stones
    along the roads: pale granite, painted red and white round the corners
    of the circuit from Ostende to the tunnel. Elsewhere (gardens, walls)
    a plain edge."""
    if SW is None:
        return 0
    edge = np.array(hexrgb("#bdb5a6"))
    granite = np.array(hexrgb(KERB_GRANITE))
    corner = lap_corners()
    roads = SW["roads"]
    pale = SW["pale"]
    n_ = 0
    kerb_m = rw_m = 0.0
    steep = []
    for pg in to_polys(SW["poly"]):
        pg = orient(pg, 1.0)
        for part, sty in ((pg.difference(pale), PAVER), (pg.intersection(pale), SLAB)):
            for q in to_polys(part.buffer(0)):
                q = shapely.set_precision(q.simplify(0.03), 0.01)
                if q.is_empty or q.geom_type != "Polygon" or q.area < 0.5 or not q.is_valid:
                    continue
                tr = triangulate(orient(q, 1.0), area=8)
                if tr is None:
                    continue
                v, tri = tr
                z = SW["zw"](v[:, 0], v[:, 1])
                # No steep triangles: where two streets at different levels
                # meet inside one piece, a triangle spanning both stood up
                # as a brick wedge.
                zt = z[tri]
                st_ = (zt.max(1) - zt.min(1)) > 1.2
                if st_.any():
                    c_ = v[tri[st_]].mean(1)
                    steep.extend(c_.tolist())
                    tri = tri[~st_]
                    if not len(tri):
                        continue
                P = enu_fast(v[:, 0], v[:, 1], z)
                uv = np.column_stack([(v[:, 0] - X0) / (X1 - X0), (Y1 - v[:, 1]) / (Y1 - Y0)])
                gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=tri, style=sty)
        for ring in [pg.exterior] + list(pg.interiors):
            r = np.asarray(ring.coords)[:, :2]
            # About 1 m pieces, so the kerb follows the curve and changes
            # colour where the corner starts.
            L = LineString(r).length
            if L < 0.2:
                continue
            ss = np.linspace(0, L, max(2, int(np.ceil(L / 1.0))) + 1)
            qq = shapely.line_interpolate_point(LineString(r), ss)
            r = np.column_stack([shapely.get_x(qq), shapely.get_y(qq)])
            a, b = r[:-1], r[1:]
            Lk = np.linalg.norm(b - a, axis=1)
            keep = Lk > 0.05
            a, b = a[keep], b[keep]
            if not len(a):
                continue
            u = (b - a) / np.linalg.norm(b - a, axis=1)[:, None]
            out = np.column_stack([u[:, 1], -u[:, 0]])
            mid = (a + b) / 2
            t_ = mid + out * 0.15
            out[contains_xy(SW["poly"], t_[:, 0], t_[:, 1])] *= -1
            za, zb = SW["zw"](a[:, 0], a[:, 1]), SW["zw"](b[:, 0], b[:, 1])
            o3 = np.column_stack([out, np.zeros(len(a))])
            o1 = mid + out * 0.5
            road = contains_xy(roads, o1[:, 0], o1[:, 1])
            # Plain edge where no road is in front (gardens, walls, quays).
            k = ~road
            if k.any():
                _quad(m, enu_fast(a[k, 0], a[k, 1], za[k] - 0.4), enu_fast(b[k, 0], b[k, 1], zb[k] - 0.4),
                      enu_fast(b[k, 0], b[k, 1], zb[k]), enu_fast(a[k, 0], a[k, 1], za[k]), edge, o3[k])
            if not road.any():
                continue
            a, b, out, za, zb, o3, mid = a[road], b[road], out[road], za[road], zb[road], o3[road], mid[road]
            # Kerb stones: the face down to the road and a 25 cm top.
            _, jl = TRACK["kd"].query(mid)
            sl = jl * TRACK["step"]
            rw = corner[jl] & (sl >= UPPER[0]) & (sl <= UPPER[1]) & (track_dist(mid[:, 0], mid[:, 1]) < SW_START + 1.5)
            lens = np.linalg.norm(b - a, axis=1)
            s0 = np.r_[0.0, np.cumsum(lens)[:-1]]
            s1 = s0 + lens
            ai, bi = a - out * KERB_W, b - out * KERB_W
            zk_a, zk_b = za + 0.012, zb + 0.012
            zero, one = np.zeros(len(a)), np.ones(len(a))
            # The road in front: where it is the lap's other leg, lower down
            # (the hairpin's island), the face is a wall down to it.
            oa, ob = a + out * 0.6, b + out * 0.6
            zra = np.where(track_dist(oa[:, 0], oa[:, 1]) < TRACK_HALF + 0.6,
                           TRACK["z"][TRACK["kd"].query(oa)[1]], za - SW_KERB)
            zrb = np.where(track_dist(ob[:, 0], ob[:, 1]) < TRACK_HALF + 0.6,
                           TRACK["z"][TRACK["kd"].query(ob)[1]], zb - SW_KERB)
            tall = np.minimum(za - zra, zb - zrb) > 0.45
            if tall.any():
                # Stone wall from the road up to under the kerb's top, a
                # railing above where it is over a metre.
                tk = tall
                Ea = enu_fast(a[tk, 0], a[tk, 1], zra[tk] - 0.3)
                Eb = enu_fast(b[tk, 0], b[tk, 1], zrb[tk] - 0.3)
                hh_a, hh_b = za[tk] - zra[tk] + 0.3, zb[tk] - zrb[tk] + 0.3
                c1 = np.cumsum(np.linalg.norm(b[tk] - a[tk], axis=1))
                c0 = np.r_[0.0, c1[:-1]]
                _quad_uv(m, Ea, Eb, enu_fast(b[tk, 0], b[tk, 1], zb[tk] - 0.02),
                         enu_fast(a[tk, 0], a[tk, 1], za[tk] - 0.02),
                         np.array(hexrgb(WALL_RUBBLE)), o3[tk],
                         [np.column_stack([c0, np.zeros(tk.sum())]), np.column_stack([c1, np.zeros(tk.sum())]),
                          np.column_stack([c1, hh_b]), np.column_stack([c0, hh_a])], style=RUBBLE)
                rt = tk & (np.minimum(za - zra, zb - zrb) > 1.0)
                if rt.any():
                    railing(m, (a - out * 0.12)[rt], (b - out * 0.12)[rt], np.minimum(za, zb)[rt] + 0.01, STREET_RAIL, 0.9)
            # (On top of a wall the kerb is a plain granite coping.)
            for sel, sty in ((~rw | tall, KERB), (rw & ~tall, KERB_RW)):
                if not sel.any():
                    continue
                E = lambda p, z: enu_fast(p[sel, 0], p[sel, 1], z[sel])
                up3 = np.tile([0.0, 0.0, 1.0], (sel.sum(), 1))
                _quad_uv(m, E(a, zk_a), E(b, zk_b), E(bi, zk_b), E(ai, zk_a), granite, up3,
                         [np.column_stack([s0, zero])[sel], np.column_stack([s1, zero])[sel],
                          np.column_stack([s1, one])[sel], np.column_stack([s0, one])[sel]], style=sty)
                sel = sel & ~tall
                if not sel.any():
                    continue
                _quad_uv(m, E(a, za - 0.4), E(b, zb - 0.4), E(b, zk_b), E(a, zk_a), granite * 0.92, o3[sel],
                         [np.column_stack([s0, zero])[sel], np.column_stack([s1, zero])[sel],
                          np.column_stack([s1, zero])[sel], np.column_stack([s0, zero])[sel]], style=sty)
            kerb_m += float(lens.sum())
            rw_m += float(lens[rw].sum())
        n_ += 1
    stats["sidewalk_m2"] = int(SW["poly"].area)
    stats["kerb_stone_m"] = int(kerb_m)
    stats["kerb_red_white_m"] = int(rw_m)
    print(f"kerbs: {kerb_m:.0f} m, {rw_m:.0f} m red and white; {len(steep)} steep pavement triangles dropped")
    if steep:
        st_s = lap_s(np.array(steep)[:, 0], np.array(steep)[:, 1])
        print("  at lap m:", sorted(set((st_s // 20 * 20).astype(int).tolist()))[:40])
    return n_


# ----------------------------------------------------------- starting grid ----
# The start line and the 20 grid slots behind it on Boulevard Albert 1er,
# painted on the film set's road as on the tours' modelled track
# (clean-track.js GRID): 8 m apart, alternating sides, pole on the left.
GRID_FIRST, GRID_GAP, GRID_SLOTS, GRID_OFF, GRID_HALF = 6.0, 8.0, 20, 2.4, 1.15


def start_grid(m, stats):
    if TRACK is None:
        return 0
    t = TRACK
    n = len(t["x"])
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    L = n * step
    dx = np.gradient(np.r_[t["x"][-1], t["x"], t["x"][0]])[1:-1]
    dy = np.gradient(np.r_[t["y"][-1], t["y"], t["y"][0]])[1:-1]
    ln = np.hypot(dx, dy) + 1e-9
    white = np.array(hexrgb("#ecebe6"))

    def at(b):
        """Point, forward and left vectors and road height b metres before the line."""
        f = ((L - b) % L) / step
        i = int(f) % n
        j = (i + 1) % n
        u = f - int(f)
        x = t["x"][i] * (1 - u) + t["x"][j] * u
        y = t["y"][i] * (1 - u) + t["y"][j] * u
        z = t["z"][i] * (1 - u) + t["z"][j] * u
        fw = np.array([dx[i], dy[i]]) / ln[i]
        return np.array([x, y]), fw, np.array([-fw[1], fw[0]]), z

    def patch(b0, b1, w0, w1):
        """A painted rectangle from b0 to b1 m before the line, w0..w1 m
        left of the centre (negative: right)."""
        c0, f0, l0, z0 = at(b0)
        c1, f1, l1, z1 = at(b1)
        pts = [c0 + l0 * w0, c0 + l0 * w1, c1 + l1 * w1, c1 + l1 * w0]
        P = enu_fast(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]),
                     np.array([z0, z0, z1, z1]) - 0.035)
        up = np.tile([0.0, 0.0, 1.0], (1, 1))
        _quad(m, P[None, 0], P[None, 1], P[None, 2], P[None, 3], white, up)

    patch(-0.175, 0.175, -TRACK_HALF + 0.2, TRACK_HALF - 0.2)       # the line
    for k in range(GRID_SLOTS):
        b = GRID_FIRST + k * GRID_GAP
        c = (1 if k % 2 == 0 else -1) * GRID_OFF                  # left is +
        patch(b, b + 0.15, c - GRID_HALF, c + GRID_HALF)
        for e in (c - GRID_HALF, c + GRID_HALF):
            patch(b, b + 1.1, e - 0.07, e + 0.07)
    stats["grid_slots"] = GRID_SLOTS
    return GRID_SLOTS


def track_dist(x, y):
    """Distance from the track centre line (inf without a track)."""
    x, y = np.atleast_1d(x), np.atleast_1d(y)
    if TRACK is None:
        return np.full(len(x), np.inf)
    return shapely.distance(TRACK["line"], shapely.points(x, y))


def off_track(x, y, margin=0.0):
    return bool(track_dist(x, y)[0] > TRACK_HALF + 0.8 + margin)


def track_audit(named, half=TRACK_HALF - 0.2, lift=0.1, tol=-0.08, over=4.5):
    """Look along the whole lap for anything of the film set standing on the
    modelled road (5 m either side of the centre line, 10 cm above the
    terrain): ground, roofs, trees or boats poking up through the tarmac.
    Vertical rays every metre along the lap, every 40 cm across it, plus
    every vertex inside the corridor (walls have no area seen from above).
    Anything higher than `over` is a bridge the cars pass under.
    Returns a list of (s, offset, height above the road, mesh name)."""
    if TRACK is None:
        return []
    from scipy.spatial import cKDTree
    tx, ty, tz = TRACK["x"], TRACK["y"], TRACK["z"]
    seg = np.hypot(np.diff(np.append(tx, tx[0])), np.diff(np.append(ty, ty[0])))
    s0 = np.concatenate([[0], np.cumsum(seg)])
    L = s0[-1]
    sq = np.arange(0, L, 0.5)
    X = np.interp(sq, s0, np.append(tx, tx[0])); Y = np.interp(sq, s0, np.append(ty, ty[0]))
    Z = np.interp(sq, s0, np.append(tz, tz[0]))
    dx, dy = np.gradient(X), np.gradient(Y)
    ln = np.hypot(dx, dy) + 1e-9
    nx, ny = -dy / ln, dx / ln
    C = enu_fast(X, Y, Z)
    kd = cKDTree(C[:, :2])
    tun = None
    if TUNNEL is not None:
        tun = (TUNNEL["ta"] - 3, TUNNEL["tb"] + 3)
    hits = []

    def keep(s, h):
        ok = (h > lift + tol) & (h < over)
        if tun is not None:
            ok &= ~((s > tun[0]) & (s < tun[1]))
        return ok

    # Ray samples across the road.
    offs = np.arange(-half, half + 1e-6, 0.4)
    sel = np.arange(0, len(sq), 2)
    RX = (X[sel, None] + nx[sel, None] * offs).ravel(); RY = (Y[sel, None] + ny[sel, None] * offs).ravel()
    R = enu_fast(RX, RY, np.repeat(Z[sel], len(offs)))
    rs = np.repeat(sq[sel], len(offs)); ro = np.tile(offs, len(sel))
    for name, m in named:
        a = m.arrays()
        if a is None:
            continue
        P, I = a[0].astype(np.float64), a[4].reshape(-1, 3)
        # Vertices inside the corridor.
        d, j = kd.query(P[:, :2], distance_upper_bound=half)
        ins = np.isfinite(d)
        if ins.any():
            h = P[ins, 2] - C[j[ins], 2]
            ok = keep(sq[j[ins]], h)
            for k in np.nonzero(ok)[0]:
                hits.append((float(sq[j[ins]][k]), float(d[ins][k]), float(h[k]),
                             name + " vertex (%.0f, %.0f)" % tuple(P[ins][k, :2])))
        # Triangles over the corridor, bucketed on a 2 m grid.
        T = P[I]
        cen = T[:, :, :2].mean(1)
        rad = np.max(np.linalg.norm(T[:, :, :2] - cen[:, None], axis=2), 1)
        dc, _ = kd.query(cen, distance_upper_bound=60)
        near = np.isfinite(dc) & (dc < rad + half + 0.5)
        T = T[near]
        if not len(T):
            continue
        lo = np.floor(T[:, :, :2].min(1) / 2).astype(int); hi = np.floor(T[:, :, :2].max(1) / 2).astype(int)
        buckets = {}
        for k in range(len(T)):
            if (hi[k] - lo[k]).max() > 40:
                continue
            for bx in range(lo[k, 0], hi[k, 0] + 1):
                for by in range(lo[k, 1], hi[k, 1] + 1):
                    buckets.setdefault((bx, by), []).append(k)
        cell = np.floor(R[:, :2] / 2).astype(int)
        for q in range(len(R)):
            ks = buckets.get((cell[q, 0], cell[q, 1]))
            if not ks:
                continue
            tt = T[ks]
            a0, a1, a2 = tt[:, 0], tt[:, 1], tt[:, 2]
            v0, v1 = a1[:, :2] - a0[:, :2], a2[:, :2] - a0[:, :2]
            w = R[q, :2] - a0[:, :2]
            den = v0[:, 0] * v1[:, 1] - v0[:, 1] * v1[:, 0]
            good = np.abs(den) > 1e-9
            den = np.where(good, den, 1)
            u = (w[:, 0] * v1[:, 1] - w[:, 1] * v1[:, 0]) / den
            v = (v0[:, 0] * w[:, 1] - v0[:, 1] * w[:, 0]) / den
            inside = good & (u >= 0) & (v >= 0) & (u + v <= 1)
            if not inside.any():
                continue
            z = (a0[:, 2] + u * (a1[:, 2] - a0[:, 2]) + v * (a2[:, 2] - a0[:, 2]))[inside]
            h = z - R[q, 2]
            ok = keep(np.full(len(h), rs[q]), h)
            if ok.any():
                hits.append((float(rs[q]), float(ro[q]), float(h[ok].max()), name))
    return hits


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


def tunnel_side_band(a, b, w0, w1):
    """Polygon on the tunnel's left (sea) side, w0..w1 m from the centre
    line, between track positions a and b."""
    ss = np.arange(a, b + 0.01, 1.0)
    x, y, _, fx, fy = tunnel_frame(ss)
    lx, ly = -fy, fx
    inner = np.column_stack([x + lx * w0, y + ly * w0])
    outer = np.column_stack([x + lx * w1, y + ly * w1])
    return Polygon(np.vstack([inner, outer[::-1]])).buffer(0)


def tunnel_open(sea):
    """The Monaco tunnel is open on its sea side, behind a row of pillars,
    where it runs along the shore under the hotel. Where the sea lies within
    35 m on the left, the set opens that wall too."""
    t = TUNNEL
    ss = np.arange(t["ta"] + 6, t["tb"] - 6, 1.5)
    x, y, _, fx, fy = tunnel_frame(ss)
    lx, ly = -fy, fx
    near = np.zeros(len(ss), bool)
    for d in np.arange(8, 36, 2.0):
        r = np.clip(((Y1 - (y + ly * d)) / RES).astype(int), 0, sea.shape[0] - 1)
        c = np.clip((((x + lx * d) - X0) / RES).astype(int), 0, sea.shape[1] - 1)
        near |= sea[r, c]
    near = ndimage.binary_closing(near, iterations=4)
    lab, n = ndimage.label(near)
    if not n:
        return
    k = np.argmax(ndimage.sum(near, lab, np.arange(1, n + 1))) + 1
    run = ss[lab == k]
    if run[-1] - run[0] < 20:
        return
    t["open"] = (float(run[0]), float(run[-1]))
    # Buildings in this band (the hotel stands on stilts over the shore)
    # start above the vault; the ground there stays below the road.
    t["open_band"] = tunnel_side_band(run[0] - 2, run[-1] + 2, TUBE_HALF - 0.3, TUBE_HALF + 30)
    t["open_ground"] = tunnel_side_band(run[0], run[-1], TUBE_HALF + 0.9, TUBE_HALF + 30)
    t["ground"] = t["ground"].difference(t["open_ground"])


def tunnel_split(m, region, zb, zt, base, fh, col, kind, style, rcol):
    """Building parts to build as usual; the part over the tunnel (and over
    its open sea side) starts above the vault."""
    parts = to_polys(region)
    if TUNNEL is None:
        return parts
    lift = TUNNEL["bld"]
    if "open_band" in TUNNEL:
        lift = lift.union(TUNNEL["open_band"])
    out = []
    for part in parts:
        if not part.intersects(lift):
            out.append(part)
            continue
        top = TUNNEL["top"]
        if zt > top + 1:
            for q in to_polys(part.intersection(lift)):
                if q.area > 2:
                    q = orient(q, 1.0)
                    add_walls(m, q, max(zb, top), zt, base, fh, col, kind, style=style)
                    add_roof(m, q, zt, rcol, False)
                    # The underside, and stilts down to the ground where
                    # the building stands over the open side.
                    if "open_band" in TUNNEL and q.intersects(TUNNEL["open_band"]) and zb < top:
                        t_ = triangulate(q)
                        if t_ is not None:
                            v_, tri_ = t_
                            P_ = enu_fast(v_[:, 0], v_[:, 1], np.full(len(v_), top))
                            m.tri(P_[tri_[:, 0]], P_[tri_[:, 2]], P_[tri_[:, 1]], np.asarray(col) * 0.8, PLAIN)
                        inner = q.buffer(-1.2)
                        bx0, by0, bx1, by1 = q.bounds
                        for gx_ in np.arange(bx0 + 3, bx1, 7.5):
                            for gy_ in np.arange(by0 + 3, by1, 7.5):
                                if inner.contains(shapely.Point(gx_, gy_)) and \
                                        TUNNEL["open_ground"].contains(shapely.Point(gx_, gy_)) and \
                                        track_dist(gx_, gy_)[0] > TUBE_HALF + 2.5:
                                    add_walls(m, box(gx_ - 0.45, gy_ - 0.45, gx_ + 0.45, gy_ + 0.45),
                                              zb - 3, top, zb, 10, np.asarray(col) * 0.92, PLAIN)
        out += [orient(q, 1.0) for q in to_polys(part.difference(lift).buffer(0)) if q.area > 2]
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
    oa, ob = t.get("open", (1e9, -1e9))
    gallery = (ss[:-1] >= oa) & (ss[1:] <= ob)
    for j in range(len(prof) - 1):
        a, b, c, d = R[j, :-1], R[j + 1, :-1], R[j, 1:], R[j + 1, 1:]
        ax = axis[:-1]
        col = ceil if prof[j][1] > 5 and prof[j + 1][1] > 5 else wall
        keep = ~gallery if j == len(prof) - 2 else np.ones(len(ax), bool)
        tri_facing(m, a[keep], b[keep], c[keep], ax[keep], col, PLAIN, TUBE_LIT)
        tri_facing(m, b[keep], d[keep], c[keep], ax[keep], col, PLAIN, TUBE_LIT)
    if gallery.any():
        tunnel_gallery(m, oa, ob, t)
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


def tunnel_gallery(m, oa, ob, t):
    """The open sea side: a low parapet, square pillars every 7.5 m and a
    lintel under the deck, with daylight (and the sea) between them."""
    W, D = TUBE_HALF, 0.8                       # wall line, wall thickness
    stone_in, stone_out = np.array(hexrgb("#d8d4c8")), np.array(hexrgb("#e2d9c6"))

    def P(s_, w, h):
        x, y, z, fx, fy = (float(v[0]) for v in tunnel_frame(np.array([float(s_)])))
        return enu_fast(np.array([x - fy * w]), np.array([y + fx * w]), np.array([z + h]))[0]

    def quad(s0, s1, w0, w1, h0, h1, toward, col, style):
        """A face between two track positions; w or h may vary, not both."""
        if w0 == w1:
            A, B, C, D_ = P(s0, w0, h0), P(s1, w0, h0), P(s0, w0, h1), P(s1, w0, h1)
        else:
            A, B, C, D_ = P(s0, w0, h0), P(s1, w0, h0), P(s0, w1, h1), P(s1, w1, h1)
        T = np.array([toward(s0), toward(s1)])
        tri_facing(m, np.array([A, B]), np.array([B, D_]), np.array([C, C]), T, col, PLAIN, style)

    inside = lambda s_: P(s_, 0, 2.8)
    outside = lambda s_: P(s_, W + 30, 2.0)
    up = lambda s_: P(s_, W + D / 2, 20)
    down = lambda s_: P(s_, W + D / 2, -20)
    deck = t["top"] - float(np.interp((oa + ob) / 2, t["s"], t["z"]))
    step = 1.5
    for s0 in np.arange(oa, ob - 0.01, step):
        s1 = min(s0 + step, ob)
        quad(s0, s1, W, W, -0.3, 1.0, inside, stone_in, TUBE_LIT)              # parapet, inside
        quad(s0, s1, W, W + D, 1.0, 1.0, up, stone_in, TUBE_LIT)               # parapet top
        quad(s0, s1, W + D, W + D, -7.0, 1.0, outside, stone_out, None)        # parapet, outside
        quad(s0, s1, W, W + D, 4.6, 4.6, down, stone_in, TUBE_LIT)             # lintel soffit
        quad(s0, s1, W + D, W + D, 4.6, deck, outside, stone_out, None)        # fascia up to the deck
    pw = 1.2
    for sp in np.arange(oa, ob + 0.01, 7.5):
        a, b = sp - pw / 2, sp + pw / 2
        quad(a, b, W, W, 1.0, 4.6, inside, stone_in, TUBE_LIT)
        quad(a, b, W + D, W + D, 1.0, 4.6, outside, stone_out, None)
        for e, sgn in ((a, -1), (b, 1)):
            A, B, C, D_ = P(e, W, 1.0), P(e, W + D, 1.0), P(e, W, 4.6), P(e, W + D, 4.6)
            T = P(e + sgn * 5, W + D / 2, 2.8)
            tri_facing(m, np.array([A, B]), np.array([B, D_]), np.array([C, C]), np.array([T, T]), stone_in, PLAIN, TUBE_LIT)


# ---------------------------------------------------------------- bridges ----
# The LiDAR ground model leaves bridges out (it shows the ravine below), so
# the set builds each OpenStreetMap bridge as a deck at the height of its
# surface in the LiDAR surface model, with pavements, parapets and piers.
BRIDGE_W = {"primary": 10, "primary_link": 7, "secondary": 8.5, "tertiary": 7.5, "residential": 6,
            "unclassified": 6, "service": 4.5, "living_street": 5, "pedestrian": 4, "footway": 2.6,
            "steps": 2.6, "cycleway": 2.6, "path": 2.4}


def _bridge_approaches(ways, ends, fam, reach=14.0):
    """The road a bridge runs on to at each end: the first `reach` metres of
    the street way that meets it there (not another bridge, not a tunnel),
    as points walking away from the bridge. None where nothing meets it."""
    out = []
    for e in ends:
        E = Point(e)
        best = None
        for t, pts, closed in ways:
            hw = t.get("highway")
            if closed or not hw or t.get("bridge") or t.get("tunnel") or hw not in BRIDGE_W:
                continue
            if (hw in ("footway", "steps", "cycleway", "path", "pedestrian")) != fam:
                continue
            if Point(pts[0]).distance(E) < 0.8:
                seq = list(pts)
            elif Point(pts[-1]).distance(E) < 0.8:
                seq = list(pts)[::-1]
            else:
                continue
            ls = LineString(seq)
            if ls.length < 3:
                continue
            best = ls if best is None or ls.length > best.length else best
        if best is None:
            out.append(None)
            continue
        L = min(reach, best.length)
        ss = np.arange(1.5, L + 0.01, 1.5)
        q = shapely.line_interpolate_point(best, ss)
        out.append(np.column_stack([shapely.get_x(q), shapely.get_y(q)]))
    return out


def bridges(m, ways, mns, mnt):
    """Each OpenStreetMap bridge: a deck at its LiDAR surface height, carried
    on a girder whose underside arches up from deep haunches at the
    abutments to a slim middle (Monaco's ravine bridges are single spans,
    like the Pont Sainte-Dévote over the church), solid abutment walls
    where the deck meets the ground, and the deck carried a few metres on
    to the street at each end so the road runs on without a step or a
    gap (the 3 m ground grid smears a ledge road on a cliff edge)."""
    mnsR, mntR = Raster(mns), Raster(mnt)
    # Lowest ground within 3 m: where the ground mesh can sag to.
    gminR = Raster(ndimage.minimum_filter(np.nan_to_num(mnt, nan=-50.0), size=7))
    n_ = 0
    asph, walk, stone = np.array(hexrgb("#55585e")), np.array(hexrgb("#d9cfbd")), np.array(hexrgb("#e4dccb"))
    concrete = np.array(hexrgb("#ddd5c3"))
    for t, pts, closed in ways:
        w = BRIDGE_W.get(t.get("highway"))
        if not w or not t.get("bridge") or closed:
            continue
        ls = LineString(pts)
        if ls.length < 4 or not box(X0, Y0, X1, Y1).buffer(-2).intersects(ls):
            continue
        foot = t.get("highway") in ("footway", "steps", "cycleway", "path", "pedestrian")
        ss = np.linspace(0, ls.length, max(3, int(ls.length / 1.5) + 1))
        q = shapely.line_interpolate_point(ls, ss)
        bx, by = shapely.get_x(q), shapely.get_y(q)
        top = mnsR.sample(bx, by)
        if np.all(np.isnan(top)):
            continue
        # Approaches: the street beyond each end, at its own ground height.
        pre, post = _bridge_approaches(ways, [pts[0], pts[-1]], foot)
        pre = pre[::-1] if pre is not None else np.zeros((0, 2))
        post = post if post is not None else np.zeros((0, 2))
        if TRACK is not None:
            # Never carry a deck on to the circuit itself.
            if len(pre):
                pre = pre[track_dist(pre[:, 0], pre[:, 1]) > TRACK_HALF + 1.0]
            if len(post):
                post = post[track_dist(post[:, 0], post[:, 1]) > TRACK_HALF + 1.0]
        x = np.concatenate([pre[:, 0], bx, post[:, 0]])
        y = np.concatenate([pre[:, 1], by, post[:, 1]])
        on = np.concatenate([np.zeros(len(pre), bool), np.ones(len(bx), bool), np.zeros(len(post), bool)])
        ok = (x > X0 + 1) & (x < X1 - 1) & (y > Y0 + 1) & (y < Y1 - 1)
        if ok[on].sum() < 3:
            continue
        x, y, on = x[ok], y[ok], on[ok]
        top = mnsR.sample(x, y)
        gnd = mntR.sample(x, y)
        gmin = gminR.sample(x, y)
        # The deck: the LiDAR surface on the bridge, the ground on the
        # approaches, without cars, lamp posts or trees, and no steps.
        z = np.where(on, top, gnd)
        z = np.where(np.isfinite(z), z, np.nanmedian(top))
        z = ndimage.median_filter(z, size=7, mode="nearest")
        z = ndimage.gaussian_filter1d(z, 2.5, mode="nearest")
        gnd = np.where(np.isfinite(gnd), gnd, z - 10)
        gmin = np.where(np.isfinite(gmin) & (gmin > -40), gmin, gnd)
        if np.nanmax((z - gnd)[on]) < 2.0:
            continue                  # a bridge over nothing much: the ground carries the street
        dx, dy = np.gradient(x), np.gradient(y)
        l = np.maximum(np.hypot(dx, dy), 1e-6)
        lx, ly = -dy / l, dx / l

        def at(off, dz=0.0):
            return enu_fast(x + lx * off, y + ly * off, z + dz)

        def at_z(off, zz):
            return enu_fast(x + lx * off, y + ly * off, zz)

        up = at(0, 30)
        hw = w / 2
        bands = [(-hw, hw, walk if foot else asph, 0.03)] if foot else \
                [(-hw + 1.4, hw - 1.4, asph, 0.03), (-hw, -hw + 1.4, walk, 0.18), (hw - 1.4, hw, walk, 0.18)]
        for o0, o1, col, dz in bands:
            A, B = at(o0, dz), at(o1, dz)
            tri_facing(m, A[:-1], B[:-1], A[1:], up[:-1], col, PLAIN)
            tri_facing(m, B[:-1], B[1:], A[1:], up[:-1], col, PLAIN)
        if not foot:
            # Kerbs: the step up from the road to each pavement.
            for o in (-hw + 1.4, hw - 1.4):
                A, B = at(o, 0.03), at(o, 0.18)
                mid_ = at(0, 0.5)
                tri_facing(m, A[:-1], B[:-1], A[1:], mid_[:-1], walk * 0.9, PLAIN)
                tri_facing(m, B[:-1], B[1:], A[1:], mid_[:-1], walk * 0.9, PLAIN)
        # The underside. Clear spans (more than 4.5 m over the ground) get
        # a girder that is deep at the abutments and slim in the middle;
        # elsewhere the deck stands on a solid wall down into the ground.
        clear = (z - gnd > 4.5) & on
        soffit = np.where(on, gmin - 0.5, gmin - 0.4)
        cum = np.concatenate([[0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
        lab, nrun = ndimage.label(clear)
        piers = []
        for k in range(1, nrun + 1):
            idx = np.nonzero(lab == k)[0]
            s0, s1 = cum[idx[0]], cum[idx[-1]]
            Ls = max(s1 - s0, 1.0)
            nsub = max(1, int(math.ceil(Ls / 48.0))) if Ls > 90 else 1
            sub = Ls / nsub
            piers += [s0 + sub * j for j in range(1, nsub)]
            u = ((cum[idx] - s0) % sub) / sub * 2 - 1
            d_end = min(4.2, 1.4 + 0.035 * sub)
            depth = 1.3 + (d_end - 1.3) * u ** 2
            soffit[idx] = np.maximum(z[idx] - depth, np.minimum(z[idx] - 1.3, gnd[idx] + 0.3))
        if TRACK is not None:
            # Over the circuit the deck keeps a slim edge: nothing hangs
            # down into the cars' way.
            over = track_dist(x, y) < TRACK_HALF + hw + 4.0
            soffit = np.where(over, np.maximum(soffit, z - 1.3), soffit)
        for side in (-1, 1):
            o = side * hw
            out_ = at(side * (hw + 20))
            # Fascia: the girder's face, down to the soffit or the ground.
            A, B = at_z(o, soffit), at(o, 0.18)
            tri_facing(m, A[:-1], B[:-1], A[1:], out_[:-1], concrete, PLAIN)
            tri_facing(m, B[:-1], B[1:], A[1:], out_[:-1], concrete, PLAIN)
            # A moulded string course along the deck edge.
            A, B = at(o + side * 0.18, -0.55), at(o + side * 0.18, -0.25)
            tri_facing(m, A[:-1], B[:-1], A[1:], out_[:-1], stone * 1.02, PLAIN)
            tri_facing(m, B[:-1], B[1:], A[1:], out_[:-1], stone * 1.02, PLAIN)
            A, B = at(o, -0.55), at(o + side * 0.18, -0.55)
            dn_ = at(0, -30)
            tri_facing(m, A[:-1], B[:-1], A[1:], dn_[:-1], stone * 0.8, PLAIN)
            tri_facing(m, B[:-1], B[1:], A[1:], dn_[:-1], stone * 0.8, PLAIN)
            A, B = at(o, -0.25), at(o + side * 0.18, -0.25)
            tri_facing(m, A[:-1], B[:-1], A[1:], up[:-1], stone, PLAIN)
            tri_facing(m, B[:-1], B[1:], A[1:], up[:-1], stone, PLAIN)
            # Parapet: outer face, inner face and coping.
            A, B = at(o, 0.18), at(o, 1.15)
            tri_facing(m, A[:-1], B[:-1], A[1:], out_[:-1], stone, PLAIN)
            tri_facing(m, B[:-1], B[1:], A[1:], out_[:-1], stone, PLAIN)
            inn = at(0, 0.6)
            A, B = at(o - side * 0.3, 0.18), at(o - side * 0.3, 1.15)
            tri_facing(m, A[:-1], B[:-1], A[1:], inn[:-1], stone * 0.95, PLAIN)
            tri_facing(m, B[:-1], B[1:], A[1:], inn[:-1], stone * 0.95, PLAIN)
            A, B = at(o - side * 0.3, 1.15), at(o + 0.05 * side, 1.15)
            tri_facing(m, A[:-1], B[:-1], A[1:], up[:-1], stone * 1.03, PLAIN)
            tri_facing(m, B[:-1], B[1:], A[1:], up[:-1], stone * 1.03, PLAIN)
        A, B = at_z(-hw, soffit), at_z(hw, soffit)
        dn = at(0, -40)
        tri_facing(m, A[:-1], B[:-1], A[1:], dn[:-1], concrete * 0.72, PLAIN)
        tri_facing(m, B[:-1], B[1:], A[1:], dn[:-1], concrete * 0.72, PLAIN)
        # The ends of the girder where the deck stops in the air.
        for i_, j_ in ((0, 1), (len(x) - 1, len(x) - 2)):
            if z[i_] - gmin[i_] < 0.6:
                continue
            off = np.array([-hw, hw, hw, -hw])
            P0 = enu_fast(x[i_] + lx[i_] * off, y[i_] + ly[i_] * off,
                          np.array([soffit[i_], soffit[i_], z[i_] + 0.18, z[i_] + 0.18]))
            T_ = enu_fast(np.array([x[i_] + (x[i_] - x[j_]) * 10]), np.array([y[i_] + (y[i_] - y[j_]) * 10]),
                          np.array([z[i_]]))
            tri_facing(m, P0[[0, 0]], P0[[1, 2]], P0[[2, 3]], np.repeat(T_, 2, 0), concrete * 0.9, PLAIN)
        # Piers only on very long viaducts: slender walls across the deck.
        for sp in piers:
            i = int(np.searchsorted(cum, sp))
            if TRACK is not None and track_dist(x[i], y[i])[0] < TRACK_HALF + 1.5:
                continue
            ax_ = np.array([lx[i], ly[i]])
            _box(m, x[i], y[i], ax_, w * 0.7, 1.6, gnd[i] - 1, soffit[i] + 0.05, concrete * 0.92, PLAIN)
        n_ += 1
    return n_


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


BLOB_V, BLOB_T = unit_sphere(7, 5)
LIMB_V, LIMB_T = cone(5)


def add_tree(m, x, y, z, h, r, kind, rnd):
    """x, y Lambert-93, z ground (NGF). Trees are built like a model
    maker's: a trunk that forks into limbs, and a crown of several leafy
    clumps (each a lumpy blob), shaded darker inside and underneath."""
    def limb(a, b, r0, r1, col):
        """A tapered branch from a to b (local metres)."""
        a, b = np.asarray(a, float), np.asarray(b, float)
        ax = b - a
        L = float(np.linalg.norm(ax))
        if L < 1e-3:
            return
        w = ax / L
        u = np.cross(w, [0.0, 0.0, 1.0] if abs(w[2]) < 0.95 else [1.0, 0.0, 0.0])
        u /= np.linalg.norm(u)
        v = np.cross(w, u)
        ring = LIMB_V[:5, :2]
        bot = a + (ring[:, :1] * u + ring[:, 1:] * v) * r0
        top = b + (ring[:, :1] * u + ring[:, 1:] * v) * r1
        P = enu_fast(x + np.r_[bot[:, 0], top[:, 0]], y + np.r_[bot[:, 1], top[:, 1]],
                     z + np.r_[bot[:, 2], top[:, 2]])
        n_ = 5
        A = [P[i] for i in range(n_)]
        tris = []
        for i in range(n_):
            j = (i + 1) % n_
            tris += [(i, j, n_ + i), (j, n_ + j, n_ + i)]
        tris = np.array(tris)
        m.tri(P[tris[:, 0]], P[tris[:, 1]], P[tris[:, 2]], col, WOOD)

    crown_c = [0.0, 0.0, 0.0]
    crown_r = 1.0

    def clump(c, rad, col, rough=0.09):
        """One leafy clump: a blob with a lumpy surface; normals point away
        from the crown's centre (soft, rounded light like a real crown)."""
        c = np.asarray(c, float)
        v = BLOB_V * (1 + rnd.normal(0, rough, (len(BLOB_V), 1)))
        pts = c + v * rad
        P = enu_fast(x + pts[:, 0], y + pts[:, 1], z + pts[:, 2])
        cc_ = np.asarray(crown_c, float)
        nrm = (pts - cc_) / np.array([crown_r[0], crown_r[0], crown_r[1]])
        nrm = 0.6 * nrm / np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-6) + 0.4 * BLOB_V
        nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
        # Light gets less far into the crown: darker inside and below.
        depth = np.linalg.norm((pts - cc_) / np.array([crown_r[0], crown_r[0], crown_r[1]]), axis=1)
        under = np.clip((pts[:, 2] - cc_[2]) / max(crown_r[1], 0.5), -1, 1)
        shade = np.clip(0.55 + 0.3 * depth + 0.17 * under, 0.45, 1.08)
        cols = np.asarray(col, np.float32)[None] * shade[:, None]
        cols = cols * (1 + rnd.normal(0, 0.035, (len(pts), 1)))
        m.add(P, nrm, cols, LEAF, idx=BLOB_T)

    bark = np.array(hexrgb(rnd.choice(["#6f5a48", "#7a6450", "#5f5044"])))
    if kind == "pine":
        # Umbrella (stone) pine: a long, slightly leaning bare trunk that
        # forks into two or three limbs under a wide, flat, dark crown.
        lean = rnd.normal(0, 0.06, 2) * h
        fork = np.array([lean[0] * 0.7, lean[1] * 0.7, h * 0.66])
        limb([0, 0, 0], fork, 0.32, 0.22, bark)
        g = np.array(hexrgb(rnd.choice(["#3f6b37", "#456f3a", "#4d7a3e", "#3b6436"]))) * (0.92 + 0.14 * rnd.random())
        top = h - r * 0.22
        crown_c[:] = [lean[0], lean[1], top]
        crown_r = (r, r * 0.35)
        nb = int(np.clip(round(r * 1.4), 4, 7))
        for i in range(nb):
            a_ = 2 * math.pi * (i + rnd.random() * 0.5) / nb
            dd = r * rnd.uniform(0.35, 0.62)
            cx_, cy_ = lean[0] + math.cos(a_) * dd, lean[1] + math.sin(a_) * dd
            if i < 3:
                limb(fork, [cx_ * 0.8 + fork[0] * 0.2, cy_ * 0.8 + fork[1] * 0.2, top - r * 0.12], 0.18, 0.1, bark)
            clump([cx_, cy_, top + rnd.normal(0, 0.15) * r * 0.3], np.array([r * 0.5, r * 0.5, r * 0.24]), g, 0.1)
        clump([lean[0], lean[1], top + r * 0.08], np.array([r * 0.55, r * 0.55, r * 0.26]), g * 1.04, 0.1)
    elif kind == "cypress":
        # Italian cypress: a tall dark flame, three stacked tapering clumps.
        limb([0, 0, 0], [0, 0, 1.4], 0.2, 0.16, bark)
        g = np.array(hexrgb(rnd.choice(["#2f5a32", "#345f36", "#2c5530"]))) * (0.92 + 0.12 * rnd.random())
        crown_c[:] = [0, 0, h * 0.55]
        crown_r = (r, h * 0.45)
        for k_, (zf, rf) in enumerate(((0.32, 1.0), (0.58, 0.82), (0.82, 0.5))):
            clump([rnd.normal(0, 0.05), rnd.normal(0, 0.05), h * zf], np.array([r * rf, r * rf, h * 0.24]), g, 0.08)
    elif kind == "palm":
        # Canary / date palm: a ringed trunk with a gentle curve, a crown of
        # long arching fronds, the lowest ones drying yellow.
        bend = rnd.normal(0, 0.05, 2) * h
        p0 = np.array([0.0, 0.0, 0.0])
        p1 = np.array([bend[0] * 0.3, bend[1] * 0.3, h * 0.5])
        p2 = np.array([bend[0], bend[1], h])
        tw = 0.28 if r < 3.5 else 0.38
        limb(p0, p1, tw * 1.25, tw, hexrgb("#8a7558"))
        limb(p1, p2, tw, tw * 0.9, hexrgb("#8f7a5c"))
        limb(p2 - [0, 0, 0.6], p2 + [0, 0, 0.5], tw * 1.25, tw * 0.7, hexrgb("#6e6a3c"))
        green = np.array(hexrgb(rnd.choice(["#5d8a3a", "#678f3f", "#557f37"])))
        dry = np.array(hexrgb("#a89a5a"))
        nf = 14
        for i in range(nf):
            a_ = 2 * math.pi * i / nf + rnd.normal(0, 0.12)
            low = i % 3 == 0
            ca, sa = math.cos(a_), math.sin(a_)
            L = r * (1.05 if not low else 0.95)
            up = 0.55 if not low else -0.1
            # Three segments arching up then down.
            ts = np.array([0.0, 0.35, 0.7, 1.0])
            prof = np.column_stack([ts * L, (up * np.sin(ts * math.pi * 0.8) - 1.1 * ts ** 2) * L * 0.45])
            wid = np.array([0.12, 0.55, 0.45, 0.05]) * (L / 3.2)
            sx, sy = -sa, ca
            ctr = np.column_stack([p2[0] + ca * prof[:, 0], p2[1] + sa * prof[:, 0], p2[2] + 0.3 + prof[:, 1]])
            Lp = ctr + np.column_stack([sx * wid, sy * wid, np.zeros(4) - 0.05])
            Rp = ctr - np.column_stack([sx * wid, sy * wid, np.zeros(4) + 0.05])
            P = enu_fast(x + np.r_[Lp[:, 0], Rp[:, 0]], y + np.r_[Lp[:, 1], Rp[:, 1]], z + np.r_[Lp[:, 2], Rp[:, 2]])
            col = (dry if low and rnd.random() < 0.6 else green) * (0.9 + 0.2 * rnd.random())
            for k_ in range(3):
                for tri_ in ((k_, 4 + k_, k_ + 1), (k_ + 1, 4 + k_, 5 + k_)):
                    m.tri(P[[tri_[0]]], P[[tri_[1]]], P[[tri_[2]]], col, LEAF)
                    m.tri(P[[tri_[0]]], P[[tri_[2]]], P[[tri_[1]]], col * 0.85, LEAF)
    else:
        # Broadleaf (plane trees on the boulevards, olives, holm oaks): a
        # trunk that forks into a few limbs, a crown of overlapping clumps.
        # The crown fills the top 55-65 % of the tree, as on real street
        # trees (pruned up to clear the pavement), not a ball on a pole.
        tz = min(max(h - 2 * r * 0.95, h * 0.3), h * 0.45)
        lean = rnd.normal(0, 0.04, 2) * h
        fork = np.array([lean[0], lean[1], tz + (h - tz) * 0.2])
        limb([0, 0, 0], fork, max(0.2, r * 0.11), max(0.14, r * 0.07), bark)
        base_g = np.array(hexrgb(rnd.choice(["#5c7a3c", "#66813f", "#56733a", "#6d8446", "#5b7547",
                                             "#748450", "#61803f"])))
        base_g = base_g * (0.9 + 0.15 * rnd.random())
        rz = (h - tz) / 2
        cz = tz + rz
        crown_c[:] = [lean[0], lean[1], cz]
        crown_r = (r, rz)
        nb = 3 if r < 2.2 else (4 if r < 3.5 else 6)
        for i in range(nb):
            a_ = 2 * math.pi * (i + rnd.random() * 0.6) / nb
            dd = r * rnd.uniform(0.3, 0.5)
            c_ = [lean[0] + math.cos(a_) * dd, lean[1] + math.sin(a_) * dd, cz + rnd.uniform(-0.3, 0.25) * rz]
            if r > 2.2 and i < 3:
                limb(fork, [c_[0] * 0.7 + fork[0] * 0.3, c_[1] * 0.7 + fork[1] * 0.3, cz - r * 0.15],
                     max(0.1, r * 0.05), 0.06, bark)
            rr_ = r * rnd.uniform(0.5, 0.66)
            clump(c_, np.array([rr_, rr_, rz * rnd.uniform(0.55, 0.7)]), base_g * (0.94 + 0.12 * rnd.random()))
        clump([lean[0], lean[1], cz + rz * 0.35], np.array([r * 0.58, r * 0.58, rz * 0.6]), base_g * 1.05)


def no_tree_zones(ways):
    """Pools, pool decks, sports grounds and piers: open paved places
    where the LiDAR sees diving boards, stands, awnings and masts standing
    up, but no trees grow."""
    out = []
    for tg, pts, closed in ways:
        if not closed or len(pts) < 4:
            continue
        if (tg.get("leisure") in ("sports_centre", "swimming_pool", "pitch", "stadium", "track")
                or tg.get("amenity") == "spring_board" or tg.get("man_made") in ("pier", "tower")):
            g = Polygon(pts).buffer(0)
            if g.area > 4:
                out.append(g.buffer(1.0))
    if TRACK is not None:
        out += [crowd_free(a, b, 32.0) for a, b in TREELESS]
        out += [crowd_free(a, b, TRACK_HALF + 10.0) for a, b in CROWD_ZONES]
    if QA is not None:
        out.append(QA["poly"].buffer(3.0))         # quai_albert() plants its own pines
    return unary_union(out) if out else None


def trees(m, mns, mnt, ortho, bmask, osm_pts, stats, no_tree=None):
    chm = np.nan_to_num(mns - mnt, nan=0)
    chm[bmask > 0] = 0
    if no_tree is not None:
        img = Image.new("L", (chm.shape[1], chm.shape[0]), 0)
        dz = ImageDraw.Draw(img)
        for q in to_polys(no_tree):
            dz.polygon([((x - X0) / RES, (Y1 - y) / RES) for x, y in q.exterior.coords], fill=255)
        chm[np.asarray(img) > 0] = 0
    o = ortho.astype(np.float32)
    exg = 2 * o[..., 1] - o[..., 0] - o[..., 2]
    lum = o.mean(-1)
    # Green in the photo, or dark and still a little green (a crown in its
    # own shade). Dark alone was too loose: building shadows on squares,
    # awnings and parked lorries came out as trees.
    veg = (chm > 2.5) & ((exg > 12) | ((lum < 60) & (exg > 3)))
    veg = ndimage.binary_opening(veg, iterations=1)
    lab, n = ndimage.label(veg)
    sizes = ndimage.sum(veg, lab, np.arange(1, n + 1))
    # At least 6 m² of crown, and green on average.
    gmean = ndimage.mean(exg, lab, np.arange(1, n + 1))
    veg &= np.isin(lab, np.nonzero((sizes >= 24) & (gmean > 8))[0] + 1)
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
        if not off_track(x, y, 0.5 * rad):
            continue
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
        if not off_track(x, y, 1.5):
            continue
        if no_tree is not None and no_tree.contains(Point(x, y)):
            continue
        z = float(mntR.sample(np.array([x]), np.array([y]))[0])
        if not np.isfinite(z):
            continue
        kind = "pine" if t.get("leaf_type") == "needleleaved" else "broad"
        # Size from the LiDAR canopy over the mapped trunk (the plane trees
        # along the boulevards are 10-14 m), crowns may lean over the
        # pavement but stop at the barriers.
        r_, c_ = int((Y1 - y) / RES), int((x - X0) / RES)
        hh = float(np.nanmax(chm[max(0, r_ - 3):r_ + 4, max(0, c_ - 3):c_ + 4])) if 0 <= r_ < chm.shape[0] and 0 <= c_ < chm.shape[1] else 0
        h_ = float(np.clip(hh, 6, 16)) if np.isfinite(hh) and hh > 4 else (9 if kind == "pine" else 7)
        rad_ = float(np.clip(0.33 * h_, 2.5, 5.0))
        if TRACK is not None:
            rad_ = float(min(rad_, max(2.0, track_dist(x, y)[0] - (TRACK_HALF + 0.5))))
        add_tree(m, x, y, z, h_, rad_, kind, rnd)
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


def yachts(m, ortho, sea, stats, ways=()):
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
    # Piers and pontoons (built as decks by piers()): white shapes on them
    # are tents, kiosks and roofs, never boats.
    pim = Image.new("L", (sea.shape[1], sea.shape[0]), 0)
    dpi = ImageDraw.Draw(pim)
    for t, pts_, closed in ways:
        if t.get("man_made") not in ("pier", "breakwater", "quay") and t.get("floating") != "yes":
            continue
        g = Polygon(pts_) if closed and len(pts_) > 3 else LineString(pts_).buffer(1.25, cap_style=2)
        for q in to_polys(g.buffer(1.0)):
            dpi.polygon([((x - X0) / RES, (Y1 - y) / RES) for x, y in q.exterior.coords], fill=255)
    on_pier = np.asarray(pim) > 0
    rnd = np.random.default_rng(5)
    # Where boats tie up: quays, pontoons and piers. Distance to them, the
    # nearest one, and their edge (for the line of the quay).
    moor = ~sea | on_pier
    moor_d, moor_i = ndimage.distance_transform_edt(~moor, return_indices=True)
    moor_d = moor_d * RES
    moor_edge = moor & ~ndimage.binary_erosion(moor)

    placed = []

    def build(stern, ax, L, W):
        side = np.array([-ax[1], ax[0]])

        def plan(along0, along1, half):
            q = [(stern + ax * a + side * s) for a, s in
                 [(along0, -half), (along1, -half), (along1, half), (along0, half)]]
            return orient(Polygon(q), 1.0)

        hull = orient(Polygon([stern + ax * u + side * s for u, s in hull_outline(L, W)]), 1.0)
        if not hull.is_valid:
            hull = hull.buffer(0)
        if not isinstance(hull, Polygon):
            return
        # Two rows meeting at a quay corner: no boat inside another.
        for h_ in placed:
            if h_.intersects(hull) and h_.intersection(hull).area > 0.08 * min(h_.area, hull.area):
                return
        placed.append(hull)
        hull_h = min(0.8 + 0.045 * L, 4.5)
        white = np.array(hexrgb(rnd.choice(["#f6f5f1"] * 5 + ["#eceae4", "#eceae4", "#2a3c55"])))
        dark_hull = white.mean() < 100
        stripe = np.array(hexrgb(rnd.choice(["#1f2a3a", "#1f2a3a", "#0e3a5c", "#6d7780"]))) if not dark_hull else np.array(hexrgb("#f2efe8"))
        top = np.array(hexrgb("#f6f5f1"))
        glass = np.array(hexrgb("#1a2430"))
        # The hull, lofted: narrower at the waterline than at the deck (the
        # sides flare out and catch the light), the sheer rising to the bow,
        # a dark boot stripe at the water, a bulwark round the deck.
        n_ = len(hull_outline(L, W))
        wl = np.array(hull_outline(L * 0.9, W * 0.82)) + [0.015 * L, 0]
        dk = np.array(hull_outline(L, W))
        sheer = lambda u: hull_h + 0.22 * hull_h * np.clip(u / L, 0, 1) ** 2
        to_xy = lambda q: stern[None] + q[:, :1] * ax[None] + q[:, 1:2] * side[None]
        WL, DK = to_xy(wl), to_xy(dk)
        loft(m, WL, np.full(n_, -0.4), WL, np.full(n_, 0.3), stripe * 0.9, HULL)
        loft(m, WL, np.full(n_, 0.3), DK, sheer(dk[:, 0]), white, HULL)
        zdk = sheer(dk[:, 0])
        cen = to_xy(np.array([[0.45 * L, 0.0]]))[0]
        fan(m, DK, zdk, cen, hull_h + 0.06 * hull_h, hexrgb("#e9e6de") if not dark_hull else hexrgb("#d9d4c8"), HULL)
        if L > 8:
            inner = to_xy(np.array(hull_outline(L * 0.985, W * 0.94)) + [0.01 * L, 0])
            loft(m, DK, zdk, DK, zdk + 0.55, white, HULL)
            loft(m, inner, zdk + 0.55, inner, zdk, white * 0.92, HULL)     # inside face
        if L > 9:
            aft = plan(0.03 * L, 0.2 * L, W * 0.4).intersection(hull.buffer(-0.4))
            for q in to_polys(aft):
                add_roof(m, orient(q, 1.0), hull_h + 0.05, hexrgb("#b88a5c"), False)   # teak
        # Superstructure: decks stepping back, each with a rounded, raked
        # front like a real motor yacht's (a hull-shaped plan, not a box),
        # a band of dark glass running round (the shader's window band on
        # HULL walls) and an overhanging roof; a radar arch on top.
        tiers = 1 if L < 12 else 2 if L < 24 else 3 if L < 45 else 4
        z = hull_h + 0.05
        for t_ in range(tiers):
            a0 = (0.17 + 0.07 * t_) * L
            a1 = (0.74 - 0.05 * t_) * L
            half = W / 2 * (0.86 - 0.09 * t_)
            if a1 - a0 < 2.2 or half < 0.6:
                break
            th = 2.4 if L > 14 else 1.7
            rake = min(1.6, 0.45 * th + 0.02 * L)
            lo_p = np.array(hull_outline(a1 - a0, 2 * half)) + [a0, 0]
            hi_p = np.array(hull_outline(a1 - a0 - rake, 2 * half - 0.35)) + [a0 + 0.1, 0]
            lo, hi = to_xy(lo_p), to_xy(hi_p)
            loft(m, lo, np.full(len(lo), z), hi, np.full(len(hi), z + th),
                 top if t_ else white if not dark_hull else top, HULL, band=True)
            roof = Polygon(hi).buffer(0)
            if isinstance(roof, Polygon):
                add_roof(m, orient(roof, 1.0), z + th, top * 0.96, False)
                # The roof's overhang: a thin slab a little wider than the deck,
                # reaching further aft over the open deck behind.
                ovp = to_xy(np.array(hull_outline(a1 - a0 - rake + 0.5, 2 * half)) + [a0 - (0.06 * L if t_ == 0 else 0.6), 0])
                ov = Polygon(ovp).buffer(0)
                if isinstance(ov, Polygon):
                    ov = orient(ov, 1.0)
                    add_walls(m, ov, z + th, z + th + 0.14, z, 100, top * 0.9, PLAIN)
                    add_roof(m, ov, z + th + 0.14, top * 0.97, False)
            z += th + 0.14
        if L > 16:
            # Radar arch: two raked legs and a bar over the top deck.
            c_ = to_xy(np.array([[(0.5 + 0.02 * tiers) * L, 0.0]]))[0]
            hw_ = W / 2 * 0.45
            ah = min(2.6, 0.9 + 0.035 * L)
            col_a = np.array(hexrgb("#ecebe6"))
            for sgn in (-1, 1):
                _box(m, *(c_ + side * sgn * hw_), ax, 0.5, 0.25, z, z + ah, col_a)
            _box(m, *c_, ax, 0.5, 2 * hw_ + 0.25, z + ah - 0.3, z + ah, col_a)
            if L > 30:
                _box(m, *c_, ax, 0.25, 0.25, z + ah, z + ah + min(3.5, 0.06 * L), np.array(hexrgb("#dedad2")))
        stats["yachts"] += 1

    lab, n = ndimage.label(boat)
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        rr, cc = np.nonzero(lab[sl] == i)
        if len(rr) < 40 or len(rr) > 60000:
            continue
        rr = rr + sl[0].start; cc = cc + sl[1].start
        if on_pier[rr, cc].mean() > 0.15:
            continue
        if MT is not None and MT["mask"][rr, cc].mean() > 0.2:
            continue                     # Mareterra: the 2022 photo shows work barges
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
        if fill > 0.9 and L > 12:
            continue                     # a crisp rectangle: a tent or roof on a quay, not a hull
        W = min(Wr, L / 2.6)
        mid = cen + ax * (proj.max() + proj.min()) / 2
        for stern, ax_, L_, W_ in moor_boats(pts, mid, ax, L, W, moor_d, moor_i, moor_edge):
            build(stern, ax_, L_, W_)
    # Mareterra's marina (Port du Petit Portier, opened 2024): the photo
    # predates it, so its berths are laid out here: yachts of 12-30 m
    # moored stern-to along the quays, bows to the middle of the basin,
    # some berths empty.
    if MT is not None:
        mm = np.zeros(sea.shape, bool)
        for t, pts_, closed in ways:
            if closed and t.get("leisure") == "marina" and len(pts_) > 3:
                g = Polygon(pts_).buffer(0)
                if MT["zone"].buffer(30).contains(g.representative_point()):
                    mm |= raster_mask(g.buffer(2.0))
        Wm = sea & mm & ~on_pier
        if Wm.sum() > 400:
            wp = cells_to_poly(Wm, X0, Y1, RES)
            wp = max(to_polys(wp), key=lambda q: q.area).simplify(0.6)
            WmR = Raster(Wm.astype(np.float32))
            moorR = Raster(moor.astype(np.float32))
            _s1 = lambda R_, q_: float(R_.sample(np.array([q_[0]]), np.array([q_[1]]))[0])
            rb = np.random.default_rng(47)
            ring_ = wp.exterior
            for s_ in np.arange(3.0, ring_.length, 5.5):
                p0 = np.array(ring_.interpolate(s_).coords[0])
                p1 = np.array(ring_.interpolate(s_ + 1.0).coords[0])
                t_ = (p1 - p0) / (np.linalg.norm(p1 - p0) + 1e-9)
                n_ = np.array([-t_[1], t_[0]])
                if _s1(WmR, p0 + n_ * 2.0) < 0.5:
                    n_ = -n_
                if _s1(WmR, p0 + n_ * 2.0) < 0.5 or _s1(moorR, p0 - n_ * 1.2) < 0.5:
                    continue                 # the basin's mouth, not a quay
                if rb.random() < 0.3:
                    continue                 # an empty berth
                free = 0.0
                while free < 80 and _s1(WmR, p0 + n_ * (free + 1.0)) > 0.5:
                    free += 1.0
                Lb = float(min(rb.choice([12, 14, 16, 18, 20, 24, 28, 30]), 0.45 * free))
                if Lb < 9:
                    continue
                build(p0 + n_ * 0.8, n_, Lb, float(np.clip(Lb / 3.4, 3.2, 7.5)))


def loft(m, A, za, B, zb, col, kind, band=False):
    """Side walls between two rings of the same length (closed: first point
    repeated or not): A at heights za, B at zb. band=True gives the wall the
    0..1 height coordinate that draws the window band on HULL walls."""
    A, B = np.asarray(A, float), np.asarray(B, float)
    za, zb = np.broadcast_to(np.asarray(za, float), (len(A),)), np.broadcast_to(np.asarray(zb, float), (len(B),))
    if not np.allclose(A[0], A[-1]):
        A, B = np.vstack([A, A[:1]]), np.vstack([B, B[:1]])
        za, zb = np.append(za, za[0]), np.append(zb, zb[0])
    # Outward winding: counter-clockwise ring (shapely orient sign=1).
    area = 0.5 * np.sum(A[:-1, 0] * A[1:, 1] - A[1:, 0] * A[:-1, 1])
    if area < 0:
        A, B, za, zb = A[::-1], B[::-1], za[::-1], zb[::-1]
    k = len(A) - 1
    pa = enu_fast(A[:, 0], A[:, 1], za)
    pb = enu_fast(B[:, 0], B[:, 1], zb)
    P = np.stack([pa[:-1], pa[1:], pb[1:], pb[:-1]], 1)
    nrm = np.cross(P[:, 1] - P[:, 0], P[:, 3] - P[:, 0])
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
    v = np.array([0.0, 0.0, 1.0, 1.0]) if band else np.zeros(4)
    uv = np.stack([np.tile([0.0, 1.0, 1.0, 0.0], (k, 1)), np.tile(v, (k, 1))], -1)
    idx = (np.arange(k)[:, None] * 4 + np.array([0, 1, 2, 0, 2, 3])[None]).ravel()
    m.add(P.reshape(-1, 3), np.repeat(nrm, 4, 0), col, kind, uv=uv.reshape(-1, 2), idx=idx)


def fan(m, ring, z, centre, zc, col, kind):
    """A deck: triangles from a centre point to each edge of the ring."""
    ring = np.asarray(ring, float)
    z = np.broadcast_to(np.asarray(z, float), (len(ring),))
    area = 0.5 * np.sum(ring[:, 0] * np.roll(ring[:, 1], -1) - np.roll(ring[:, 0], -1) * ring[:, 1])
    if area < 0:
        ring, z = ring[::-1], z[::-1]
    p = enu_fast(ring[:, 0], ring[:, 1], z)
    c = enu_fast(np.array([centre[0]]), np.array([centre[1]]), np.array([zc]))
    a, b = p, np.roll(p, -1, 0)
    m.tri(np.repeat(c, len(p), 0), a, b, col, kind)


def moor_boats(pts, mid, ax, L, W, moor_d, moor_i, moor_edge):
    """Lay a white shape on the water out as moored boats. In Port Hercule
    boats lie stern-to: bow out, perpendicular to the quay or pontoon,
    side by side. A shape touching a quay is read in the quay's frame: its
    depth out from the quay is the boat length, its width along the quay
    holds as many boats as fit side by side (neighbours merge into one
    shape in the photo). Shapes out in the open keep their own axis.
    Yields (stern point, bow direction, length, beam)."""
    rows = np.clip(((Y1 - pts[:, 1]) / RES).astype(int), 0, moor_d.shape[0] - 1)
    cols = np.clip(((pts[:, 0] - X0) / RES).astype(int), 0, moor_d.shape[1] - 1)
    d = moor_d[rows, cols]
    if d.min() > 6.0:
        # Free: bow away from the nearest quay.
        def dl(p):
            c_ = int(np.clip((p[0] - X0) / RES, 0, moor_d.shape[1] - 1)); r_ = int(np.clip((Y1 - p[1]) / RES, 0, moor_d.shape[0] - 1))
            return moor_d[r_, c_]
        if dl(mid + ax * L / 2) < dl(mid - ax * L / 2):
            ax = -ax
        yield mid - ax * L / 2, ax, L, W
        return
    k = int(np.argmin(d))
    qr, qc = moor_i[0][rows[k], cols[k]], moor_i[1][rows[k], cols[k]]
    q0 = np.array(cell_xy(qr, qc), float)
    # The line of the quay: its edge cells within 12 m of where the boat
    # touches it.
    w_ = int(12 / RES)
    r0, r1 = max(0, qr - w_), min(moor_d.shape[0], qr + w_ + 1)
    c0, c1 = max(0, qc - w_), min(moor_d.shape[1], qc + w_ + 1)
    er, ec = np.nonzero(moor_edge[r0:r1, c0:c1])
    tdir = None
    if len(er) > 10:
        ex, ey = cell_xy(er + r0, ec + c0)
        e = np.column_stack([ex, ey])
        e = e[np.hypot(e[:, 0] - q0[0], e[:, 1] - q0[1]) < 12]
        if len(e) > 10:
            ev, evec = np.linalg.eigh(np.cov((e - e.mean(0)).T))
            if ev[1] > 4 * max(ev[0], 1e-6):
                tdir = evec[:, 1]
    if tdir is None:
        # A corner or a ragged edge: square to the shape's own axis.
        tdir = np.array([-ax[1], ax[0]])
    nrm = np.array([-tdir[1], tdir[0]])
    if (mid - q0) @ nrm < 0:
        nrm = -nrm
    a = (pts - q0) @ nrm
    b = (pts - q0) @ tdir
    depth = a.max() - max(a.min(), 0.0) + RES
    width = b.max() - b.min() + RES
    if depth < 4.5 and width > 2.5 * depth:
        # Lying alongside the quay.
        Lb = min(width, 70.0)
        yield (q0 + nrm * (max(a.min(), 0.6) + depth / 2) + tdir * (b.min() + (width - Lb) / 2),
               tdir, Lb, min(depth, Lb / 3.2))
        return
    Lb = float(np.clip(depth, 5.0, 90.0))
    beam = float(np.clip(Lb / 3.1, 2.2, 13.0))
    n_ = max(1, int(round(width / (beam + 0.9))))
    pitch = width / n_
    beam = min(beam, pitch - 0.7)
    if beam < 1.8:
        n_ = max(1, int(width // 2.6)); pitch = width / n_; beam = pitch - 0.7
    a_st = max(a.min(), 0.8)
    for j in range(n_):
        yield q0 + nrm * a_st + tdir * (b.min() + pitch * (j + 0.5)), nrm, Lb, beam


def pool_features(m, ways, mnt):
    """The diving tower of a public pool (OSM tower:type=diving): a concrete
    column with boards at 1, 3, 5, 7.5 and 10 m reaching out over the
    water. The tower's own LiDAR hump is kept free of trees by
    no_tree_zones()."""
    mntR = Raster(mnt)
    pools = [Polygon(p_).buffer(0) for tg, p_, c in ways if c and len(p_) > 3 and tg.get("leisure") == "swimming_pool"]
    n_ = 0
    for tg, pts, closed in ways:
        if not closed or len(pts) < 4 or tg.get("tower:type") != "diving":
            continue
        tw = Polygon(pts).buffer(0)
        c = tw.centroid
        near = min(pools, key=lambda q: q.distance(c)) if pools else None
        if near is None or near.distance(c) > 15:
            continue
        z0 = float(np.nanmedian(mntR.sample(np.array([c.x]), np.array([c.y]))))
        if not np.isfinite(z0):
            continue
        H = float(tg.get("height", 10))
        concrete, board = np.array(hexrgb("#e9e5db")), np.array(hexrgb("#f4f1ea"))
        rail = np.array(hexrgb("#3b5f8a"))
        core = orient(tw.buffer(-0.2, join_style=2), 1.0)
        add_walls(m, core, z0 - 0.3, z0 + H + 1.0, z0, 100, concrete, PLAIN)
        add_roof(m, core, z0 + H + 1.0, concrete * 0.97, False)
        # Out over the pool: boards perpendicular to the pool's near side.
        pc = near.representative_point()
        out = np.array([pc.x - c.x, pc.y - c.y])
        bnd = near.exterior
        q = bnd.interpolate(bnd.project(c))
        if q.distance(c) > 0.5:
            out = np.array([q.x - c.x, q.y - c.y])
        out /= np.linalg.norm(out) + 1e-9
        side = np.array([-out[1], out[0]])
        rr = np.asarray(tw.exterior.coords)[:, :2] - [c.x, c.y]
        edge = float((rr @ out).max())
        for k, (hb, reach, wb) in enumerate([(1.0, 4.0, 0.5), (3.0, 4.5, 0.6), (5.0, 5.0, 1.8),
                                             (7.5, 5.5, 2.0), (H, 6.0, 2.2)]):
            sx = ((k % 2) * 2 - 1) * 0.8 if hb < 4 else 0.0
            a = np.array([c.x, c.y]) + out * (edge - 0.3) + side * sx
            b = a + out * reach
            slab = orient(Polygon([a - side * wb / 2, b - side * wb / 2, b + side * wb / 2, a + side * wb / 2]), 1.0)
            th = 0.12 if hb < 4 else 0.35
            add_walls(m, slab, z0 + hb - th, z0 + hb, z0, 100, board if hb < 4 else concrete, PLAIN)
            add_roof(m, slab, z0 + hb, board, False)
            if hb >= 4:
                # Railings along both sides, stopping short of the edge.
                for s_ in (-1, 1):
                    r0 = a + side * s_ * (wb / 2 - 0.05)
                    r1 = r0 + out * (reach - 1.2)
                    rl = orient(Polygon([r0 - side * 0.04, r1 - side * 0.04, r1 + side * 0.04, r0 + side * 0.04]), 1.0)
                    add_walls(m, rl, z0 + hb, z0 + hb + 1.0, z0, 100, rail, PLAIN)
        n_ += 1
    return n_


# PLAIN styles drawn by the viewer: 5 polished steel, 6 bronze.
METAL, BRONZE = 5, 6


def _box(m, cx, cy, ax, w, d, z0, z1, col, kind=PLAIN, style=None, top=True):
    """An upright box centred on (cx, cy), w along ax and d across."""
    ax = np.asarray(ax, float) / (np.linalg.norm(ax) + 1e-9)
    sd = np.array([-ax[1], ax[0]])
    c = np.array([cx, cy])
    pts = [c - ax * w / 2 - sd * d / 2, c + ax * w / 2 - sd * d / 2, c + ax * w / 2 + sd * d / 2, c - ax * w / 2 + sd * d / 2]
    poly = orient(Polygon(pts), 1.0)
    add_walls(m, poly, z0, z1, z0, 100, col, kind, style=style, min_edge=0.01)
    if top:
        P = enu_fast(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]), np.full(4, z1))
        m.tri(P[[0, 0]], P[[1, 2]], P[[2, 3]], col, kind, style=style)


def _ball(m, cx, cy, z, r, col, style, nu=10, nv=6):
    v, t = unit_sphere(nu, nv)
    P = enu_fast(cx + v[:, 0] * r, cy + v[:, 1] * r, z + v[:, 2] * r)
    Q = enu_fast(cx + v[:, 0] * (r + 1), cy + v[:, 1] * (r + 1), z + v[:, 2] * (r + 1))
    nrm = Q - P
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
    m.add(P, nrm, col, PLAIN, idx=t, style=style)


def street_features(m, ways, pts, mnt, stats):
    """The furniture of the streets around the circuit, from OpenStreetMap:
    fountain basins with a stone rim and water jets, the bronze statues and
    busts on their plinths (Fangio, Louis Chiron, Massenet, Berlioz...),
    Anish Kapoor's Sky Mirror on Casino square, the Chrome Ball, and the
    rows of flagpoles flying Monaco's red and white. Small things, but from
    the lap camera they give the streets their scale and depth."""
    mntR = Raster(mnt)
    xmin, ymin, xmax, ymax = X0, Y1 - mnt.shape[0] * RES, X0 + mnt.shape[1] * RES, Y1
    inside = lambda x, y: xmin + 5 < x < xmax - 5 and ymin + 5 < y < ymax - 5

    def gz(x, y):
        z = mntR.sample(np.array([x]), np.array([y]))
        z = float(z[0]) if np.size(z) else float("nan")
        return z

    stone, rimc = np.array(hexrgb("#ddd5c4")), np.array(hexrgb("#cfc5b0"))
    water, jet = np.array(hexrgb("#3f9fae")), np.array(hexrgb("#eef6f7"))
    bronze, steel = np.array(hexrgb("#6a5638")), np.array(hexrgb("#d8dee4"))
    n = {"fountains": 0, "statues": 0, "flags": 0, "sky_mirror": 0}

    # Fountains: a stone rim round the basin, water just below its top and,
    # in the bigger ones, a cluster of jets.
    for t, p_, closed in ways:
        if not closed or len(p_) < 4 or t.get("amenity") != "fountain":
            continue
        g = Polygon(p_).buffer(0)
        c = g.centroid
        if g.area < 4 or not inside(c.x, c.y) or not off_track(c.x, c.y, 1.0):
            continue
        ring = np.asarray(g.exterior.coords)
        zr = mntR.sample(ring[:, 0], ring[:, 1])
        if not np.isfinite(zr).any():
            continue
        z0 = float(np.nanmedian(zr))
        inner = g.buffer(-0.45)
        if inner.is_empty or inner.geom_type != "Polygon":
            continue
        rim = 0.55
        add_walls(m, orient(g, 1.0), z0 - 0.3, z0 + rim, z0, 100, rimc, PLAIN)
        add_walls(m, orient(inner, 1.0), z0, z0 + rim, z0, 100, rimc * 0.93, PLAIN, inward=True)
        for poly, z, col in ((g.difference(inner), z0 + rim, stone), (inner, z0 + rim - 0.12, water)):
            tr = triangulate(poly)
            if tr is None:
                continue
            v, tri = tr
            P = enu_fast(v[:, 0], v[:, 1], np.full(len(v), z))
            m.tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], col, PLAIN)
        if g.area > 30 and not any(tt.get("name") == "Sky Mirror" and g.buffer(1).contains(Point(*xy))
                                   for xy, tt in pts):
            r = math.sqrt(inner.area / math.pi)
            _box(m, c.x, c.y, (1, 0), 0.9, 0.9, z0, z0 + 0.9, stone)
            for k in range(7):
                a = 2 * math.pi * k / 7
                rr = 0 if k == 0 else r * 0.45
                h = 2.6 if k == 0 else 1.2
                x, y = c.x + rr * math.cos(a), c.y + rr * math.sin(a)
                P = enu_fast(x + CONE_V[:, 0] * 0.12, y + CONE_V[:, 1] * 0.12, z0 + rim - 0.12 + CONE_V[:, 2] * h)
                m.tri(P[CONE_T[:, 0]], P[CONE_T[:, 1]], P[CONE_T[:, 2]], jet, PLAIN)
        n["fountains"] += 1

    for (x, y), t in pts:
        if not inside(x, y):
            continue
        name = t.get("name") or ""
        typ = t.get("artwork_type") or t.get("memorial") or ""
        z0 = gz(x, y)
        if not np.isfinite(z0) or not off_track(x, y, 0.5):
            continue
        # Face the nearest stretch of the circuit (or south, to the sea).
        if TRACK is not None:
            q = TRACK["line"].interpolate(TRACK["line"].project(shapely.Point(x, y)))
            fa = np.array([q.x - x, q.y - y])
        else:
            fa = np.array([0.0, -1.0])
        fa = fa / (np.linalg.norm(fa) + 1e-9)
        sd = np.array([-fa[1], fa[0]])
        if name == "Sky Mirror":
            # A 5.5 m concave disc of polished steel, tilted back to the sky,
            # turned to the cars coming round the square from Massenet.
            fa = np.array([1.0, 0.25]) / math.hypot(1.0, 0.25)
            sd = np.array([-fa[1], fa[0]])
            R, depth, tilt = 2.75, 0.45, math.radians(20)
            zc = z0 + 0.6 + R
            up = np.array([0.0, 0.0, 1.0])
            nrm_ax = np.array([fa[0] * math.cos(tilt), fa[1] * math.cos(tilt), math.sin(tilt)])
            e1 = np.array([sd[0], sd[1], 0.0])
            e2 = np.cross(nrm_ax, e1)
            e2 /= np.linalg.norm(e2)
            nr, nu_ = 6, 32
            V, N = [], []
            for i in range(nr + 1):
                rr = R * i / nr
                for j in range(nu_):
                    a = 2 * math.pi * j / nu_
                    off = e1 * rr * math.cos(a) + e2 * rr * math.sin(a) + nrm_ax * depth * (rr / R) ** 2
                    V.append(off)
                    sl = 2 * depth * rr / R ** 2
                    rad = e1 * math.cos(a) + e2 * math.sin(a)
                    nv = nrm_ax - rad * sl
                    N.append(nv / np.linalg.norm(nv))
            V, N = np.array(V), np.array(N)
            P = enu_fast(x + V[:, 0], y + V[:, 1], zc + V[:, 2])
            Q = enu_fast(x + V[:, 0] + N[:, 0], y + V[:, 1] + N[:, 1], zc + V[:, 2] + N[:, 2])
            nn = (Q - P) / np.linalg.norm(Q - P, axis=1, keepdims=True)
            idx = []
            for i in range(nr):
                for j in range(nu_):
                    a_, b_ = i * nu_ + j, i * nu_ + (j + 1) % nu_
                    c_, d_ = a_ + nu_, b_ + nu_
                    idx += [a_, c_, b_, b_, c_, d_]
            idx = np.array(idx)
            # Front: concave mirror; back: the same surface, reversed.
            m.add(P, nn, steel, PLAIN, idx=idx, style=METAL)
            m.add(P, -nn, steel * 0.8, PLAIN, idx=idx.reshape(-1, 3)[:, ::-1], style=METAL)
            _box(m, x - fa[0] * 0.25, y - fa[1] * 0.25, sd, 0.7, 0.45, z0, zc - 0.05, steel * 0.7, style=METAL)
            n["sky_mirror"] += 1
        elif name == "Chrome Ball":
            _ball(m, x, y, z0 + 1.0, 1.0, steel, METAL)
            n["statues"] += 1
        elif typ == "bust" and name:
            _box(m, x, y, fa, 0.6, 0.6, z0, z0 + 1.45, stone)
            _box(m, x, y, sd, 0.58, 0.3, z0 + 1.45, z0 + 1.78, bronze, style=BRONZE)
            _ball(m, x, y, z0 + 1.97, 0.15, bronze, BRONZE, 8, 5)
            n["statues"] += 1
        elif typ == "statue":
            # Plinth, then a standing figure in bronze, facing the street.
            ph = 1.4
            _box(m, x, y, fa, 1.3, 1.3, z0, z0 + ph, stone)
            _box(m, x, y, fa, 1.45, 1.45, z0 + ph, z0 + ph + 0.12, rimc)
            zb = z0 + ph + 0.12
            for s_ in (-1, 1):
                lx, ly = x + sd[0] * 0.12 * s_, y + sd[1] * 0.12 * s_
                _box(m, lx, ly, fa, 0.2, 0.17, zb, zb + 0.9, bronze, style=BRONZE)
            _box(m, x, y, sd, 0.5, 0.3, zb + 0.9, zb + 1.52, bronze, style=BRONZE)
            for s_ in (-1, 1):
                ax_, ay_ = x + sd[0] * 0.31 * s_, y + sd[1] * 0.31 * s_
                _box(m, ax_, ay_, fa, 0.13, 0.12, zb + 0.95, zb + 1.5, bronze, style=BRONZE)
            _ball(m, x, y, zb + 1.7, 0.14, bronze, BRONZE, 8, 5)
            n["statues"] += 1
        elif t.get("man_made") == "flagpole":
            H = 9.0
            _box(m, x, y, (1, 0), 0.12, 0.12, z0, z0 + H, np.array(hexrgb("#f0f0ee")))
            _ball(m, x, y, z0 + H + 0.08, 0.1, np.array(hexrgb("#e2c46a")), METAL, 6, 4)
            # Monaco: red over white, flying downwind of the sea breeze.
            fw, fh = 1.5, 1.0
            wdir = np.array([-0.3, 1.0]) / math.hypot(0.3, 1.0)
            fx, fy = x + wdir[0] * (fw / 2 + 0.06), y + wdir[1] * (fw / 2 + 0.06)
            top = z0 + H - 0.15
            _box(m, fx, fy, wdir, fw, 0.03, top - fh / 2, top, np.array(hexrgb("#ce1126")))
            _box(m, fx, fy, wdir, fw, 0.03, top - fh, top - fh / 2, np.array(hexrgb("#f7f7f5")), top=False)
            n["flags"] += 1
    stats.update(n)
    return n


def pavements(m, sea, bmask, stats):
    """A raised pavement behind the barriers on both sides of the circuit:
    pale stone, 12 cm above the road, from just behind the barrier to
    2.6 m out, with its kerb face down to the ground. Up close at street
    level it gives the edge of the track height and a crisp line, where
    the painted ground was soft. Not over water, buildings, the tunnel,
    other stretches of the lap, or the Nouvelle Chicane's open side."""
    if TRACK is None:
        return 0
    t = TRACK
    n = len(t["x"])
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    seaR, bR = Raster(sea.astype(np.float32)), Raster(bmask.astype(np.float32))
    stone = np.array(hexrgb("#d4cbb8"))
    kerb = np.array(hexrgb("#bdb3a0"))
    d0, d1 = TRACK_HALF + 0.45, TRACK_HALF + 2.6
    count = 0
    dx = np.gradient(t["x"]); dy = np.gradient(t["y"])
    ln = np.hypot(dx, dy) + 1e-9
    nx, ny = -dy / ln, dx / ln
    s_ = np.arange(n) * step
    for side in (1, -1):
        ok = np.ones(n, bool)
        for k_, d in enumerate((d0, (d0 + d1) / 2, d1)):
            px, py = t["x"] + side * nx * d, t["y"] + side * ny * d
            ok &= (seaR.sample(px, py) < 0.5) & (bR.sample(px, py) < 0.5)
            ok &= track_dist(px, py) > TRACK_HALF + 0.3
        if TUNNEL is not None:
            ok &= ~((s_ > TUNNEL["ta"] - 6) & (s_ < TUNNEL["tb"] + 6))
        if side == -1:
            ok &= ~((s_ > 2072) & (s_ < 2164))            # the chicane's escape road (clean-track OPEN)
        if PROM is not None and side == OSTENDE_SIDE:
            ok[PROM["i0"]:PROM["i1"]] = False             # ostende_promenade()
        # Runs of at least 6 m.
        lab, nr = ndimage.label(ok)
        for r in range(1, nr + 1):
            idx = np.nonzero(lab == r)[0]
            if len(idx) < 4:
                continue
            xs_i = t["x"][idx] + side * nx[idx] * d0; ys_i = t["y"][idx] + side * ny[idx] * d0
            xs_o = t["x"][idx] + side * nx[idx] * d1; ys_o = t["y"][idx] + side * ny[idx] * d1
            z = t["z"][idx]
            pi = enu_fast(xs_i, ys_i, z + 0.12); po = enu_fast(xs_o, ys_o, z + 0.12)
            pk = enu_fast(xs_o, ys_o, z - 0.3)
            a, b = pi[:-1], pi[1:]; c, d_ = po[1:], po[:-1]
            if side == 1:
                m.tri(a, d_, c, stone, PLAIN); m.tri(a, c, b, stone, PLAIN)
            else:
                m.tri(a, b, c, stone, PLAIN); m.tri(a, c, d_, stone, PLAIN)
            # Kerb face on the outer edge, facing away from the track.
            ka, kb, kc, kd = po[:-1], po[1:], pk[1:], pk[:-1]
            if side == 1:
                m.tri(ka, kb, kc, kerb, PLAIN); m.tri(ka, kc, kd, kerb, PLAIN)
            else:
                m.tri(ka, kc, kb, kerb, PLAIN); m.tri(ka, kd, kc, kerb, PLAIN)
            count += len(idx)
    stats["pavement_m"] = int(count * step)
    return count


# ----------------------------------------------------------------- crowds ----
CROWD = 4                            # PLAIN style: spectators (the viewer animates them)
# Grand Prix crowds standing behind the barriers: lap metres (from, to).
# The harbour quay from the swimming pool to La Rascasse first (Fly), then
# the start straight, Sainte Dévote, Casino square, Tabac and Anthony Noghes.
CROWD_ZONES = [(2640, 2965), (3170, 3316), (0, 95), (165, 265), (815, 905), (2240, 2350)]
TREELESS = [(2630, 2990)]            # the quay after the pool has no trees


def crowd_free(s0, s1, width):
    """The track stretch s0..s1 (lap metres) buffered by `width`."""
    t = TRACK
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    i0, i1 = int(s0 / step), int(s1 / step) + 1
    return LineString(np.column_stack([t["x"][i0:i1], t["y"][i0:i1]])).buffer(width)



# Parts of a spectator, for colours: trousers, shins (trousers or bare
# legs), shoes, shirt, skin, hair (or cap).
P_LEGS, P_SHIN, P_FEET, P_SHIRT, P_SKIN, P_HAIR = range(6)
_PERSON = {}


def person_template(arms_up):
    """One standing person 1.72 m tall, x forward, y left, z up, made of
    smooth tubes: separate legs with shoes, a torso narrowing to the
    shoulders, a neck, a round head with hair or a cap, and arms down by
    the sides or raised. About 70 vertices and 120 triangles, so
    5,000 of them stay light on a phone. Returns positions, smooth normals, the part of
    each vertex and the triangles."""
    if arms_up in _PERSON:
        return _PERSON[arms_up]
    P, part, T = [], [], []

    def tube(centers, radii, parts, sides, cap0=None, cap1=None, twist=0.0):
        """Rings around the polyline `centers` (radii (rx, ry) each, rx along
        the body's x), joined into a tube; cap0/cap1 close an end with a
        pole at that point, in that part."""
        C = np.asarray(centers, np.float64)
        d = np.gradient(C, axis=0)
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        start = len(P)
        if cap0 is not None:
            P.append(cap0[0]); part.append(cap0[1])
        th = np.arange(sides) * 2 * np.pi / sides + twist
        for c, dd, (rx, ry), pt in zip(C, d, radii, parts):
            ref = np.array([0.0, 0.0, 1.0]) if abs(dd[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
            u = np.cross(ref, dd); u /= np.linalg.norm(u)
            if abs(dd[2]) >= 0.9:                       # vertical: u along x
                u = np.array([1.0, 0.0, 0.0]) - dd * dd[0]; u /= np.linalg.norm(u)
            v = np.cross(dd, u)
            for t in th:
                P.append(c + np.cos(t) * rx * u + np.sin(t) * ry * v); part.append(pt)
        r0 = start + (1 if cap0 is not None else 0)
        nr = len(C)
        for i in range(nr - 1):
            for j in range(sides):
                a, b = r0 + i * sides + j, r0 + i * sides + (j + 1) % sides
                c2, d2 = a + sides, b + sides
                T.extend([(a, b, d2), (a, d2, c2)])
        if cap0 is not None:
            T.extend([(start, r0 + (j + 1) % sides, r0 + j) for j in range(sides)])
        if cap1 is not None:
            pole = len(P); P.append(cap1[0]); part.append(cap1[1])
            last = r0 + (nr - 1) * sides
            T.extend([(last + j, last + (j + 1) % sides, pole) for j in range(sides)])

    for sy in (-1, 1):
        y = 0.095 * sy
        # Leg: shoe, ankle, calf, knee, thigh, hip.
        tube([(0.03, y, 0.06), (0.01, y, 0.46), (0.0, y * 0.9, 0.9)],
             [(0.075, 0.05), (0.06, 0.06), (0.095, 0.095)],
             [P_FEET, P_SHIN, P_LEGS], 4, cap0=((0.05, y, 0.0), P_FEET), twist=np.pi / 4)
    # Torso: hips, waist, chest, shoulders, then the neck.
    tube([(0.0, 0.0, 0.86), (0.01, 0.0, 1.27), (0.0, 0.0, 1.41), (0.0, 0.0, 1.5)],
         [(0.12, 0.19), (0.125, 0.2), (0.1, 0.2), (0.05, 0.055)],
         [P_LEGS, P_SHIRT, P_SHIRT, P_SKIN], 6)
    # Head, slightly forward of the neck; hair on the crown and back.
    tube([(0.015, 0.0, 1.565), (0.008, 0.0, 1.655)],
         [(0.1, 0.085), (0.1, 0.09)],
         [P_SKIN, P_HAIR], 5, cap0=((0.02, 0.0, 1.505), P_SKIN), cap1=((0.0, 0.0, 1.72), P_HAIR))
    for sy in (-1, 1):
        if arms_up:
            pts = [(0.0, 0.18 * sy, 1.4), (0.04, 0.29 * sy, 1.66), (0.03, 0.3 * sy, 1.95)]
        else:
            pts = [(0.0, 0.2 * sy, 1.4), (-0.01, 0.255 * sy, 1.11), (0.05, 0.24 * sy, 0.86)]
        tube(pts, [(0.055, 0.055), (0.045, 0.045), (0.035, 0.035)],
             [P_SHIRT, P_SKIN, P_SKIN], 4, cap1=(pts[-1], P_SKIN), twist=np.pi / 4)
    pos = np.asarray(P, np.float32)
    tri = np.asarray(T, np.int64)
    # Smooth normals, area weighted; triangles wound outwards.
    fn = np.cross(pos[tri[:, 1]] - pos[tri[:, 0]], pos[tri[:, 2]] - pos[tri[:, 0]])
    nrm = np.zeros_like(pos)
    for i in range(3):
        np.add.at(nrm, tri[:, i], fn)
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
    _PERSON[arms_up] = (pos, nrm.astype(np.float32), np.asarray(part), tri.astype(np.uint32))
    return _PERSON[arms_up]


def crowds(m, mnt, sea, bmask, stats):
    """Spectators on foot behind the barriers: a band from 1.5 m to about
    9 m beyond the barrier, on open, fairly flat ground (no buildings, no
    water, not on another stretch of the track). Each one is a smooth
    low-poly person (person_template): legs and shoes, trousers or shorts,
    a shirt in a bright colour (some in team red), a head with hair or a
    cap, and for many, arms up. uv.y = 1 marks the parts that jump and wave, uv.x
    the phase, so the viewer can animate the crowd."""
    if TRACK is None:
        return 0
    t = TRACK
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    rnd = np.random.default_rng(11)
    mntR, seaR, bR = Raster(mnt), Raster(sea.astype(np.float32)), Raster(bmask.astype(np.float32))
    P = []
    for s0, s1 in CROWD_ZONES:
        for s in np.arange(s0, s1, 1.05):
            i = int(s / step) % len(t["x"])
            j = (i + 1) % len(t["x"])
            dx, dy = t["x"][j] - t["x"][i], t["y"][j] - t["y"][i]
            ln = math.hypot(dx, dy) or 1
            nx, ny = -dy / ln, dx / ln
            for side in (1, -1):
                for d in np.arange(TRACK_HALF + 2.9, TRACK_HALF + 8.2, 1.05):
                    jx, jy = rnd.normal(0, 0.18, 2)
                    P.append((t["x"][i] + side * nx * d + jx, t["y"][i] + side * ny * d + jy, t["z"][i], s, side * nx, side * ny))
    P = np.array(P)
    x, y, zt = P[:, 0], P[:, 1], P[:, 2]
    zg = mntR.sample(x, y)
    ok = np.isfinite(zg) & (seaR.sample(x, y) < 0.5) & (bR.sample(x, y) < 0.5)
    ok &= np.abs(zg - zt) < 1.2                      # standing at the level of the road
    ok &= track_dist(x, y) > TRACK_HALF + 1.7        # not on any other stretch
    if TUNNEL is not None:
        ok &= ~contains_xy(TUNNEL["ground"].buffer(4), x, y)
    keep = ok & (rnd.random(len(P)) < 0.75)
    P, zg = P[keep], zg[keep]
    n = len(P)
    if not n:
        return 0
    base = enu_fast(P[:, 0], P[:, 1], zg)
    # Facing the track (towards -normal), a little turned either way.
    fx, fy = -P[:, 4], -P[:, 5]
    ang = np.arctan2(fy, fx) + rnd.normal(0, 0.35, n)
    # Normal vector in ENU from Lambert (the grid is turned about 3 degrees).
    rot = math.atan2(AFF[0, 1], AFF[0, 0])
    ang = ang + rot
    h = rnd.normal(1.72, 0.08, n).clip(1.5, 1.95)
    shirts = np.array([hexrgb(c) for c in ["#d0202a", "#d0202a", "#f2f2f2", "#1d3f8f", "#f4c20d", "#1a1a1a",
                                         "#2e8b57", "#ff7a00", "#e83e8c", "#7fb3e6", "#d0202a", "#ffffff"]], np.float32)
    skin = np.array([hexrgb(c) for c in ["#f1c7a5", "#d9a47c", "#a8724c", "#6b4630", "#e8b896"]], np.float32)
    trousers = np.array([hexrgb(c) for c in ["#2a3346", "#3b3b3b", "#c9b48a", "#1d2a44", "#5a6b7d", "#e8e4da"]], np.float32)
    hairs = np.array([hexrgb(c) for c in ["#2b1d14", "#14110f", "#5a3a22", "#b08a4e", "#8a8580", "#3b2a1c"]], np.float32)
    shoes = np.array([hexrgb(c) for c in ["#f2f2f2", "#1a1a1a", "#4a3a2c", "#d8d8d8"]], np.float32)
    shirt = shirts[rnd.integers(0, len(shirts), n)]
    face = skin[rnd.integers(0, len(skin), n)]
    legs = trousers[rnd.integers(0, len(trousers), n)]
    hair = hairs[rnd.integers(0, len(hairs), n)]
    feet = shoes[rnd.integers(0, len(shoes), n)]
    # Shorts for some (bare shins), team caps for some.
    shorts = rnd.random(n) < 0.35
    shin = np.where(shorts[:, None], face, legs)
    caps = rnd.random(n) < 0.25
    hair = np.where(caps[:, None], np.where((rnd.random(n) < 0.6)[:, None], hexrgb("#d0202a"), shirt), hair)
    phase = rnd.random(n)
    arms_up = rnd.random(n) < 0.4
    # Part colours, in PERSON part order.
    palette = np.stack([legs, shin, feet, shirt, face, hair], 1)          # (n, 6, 3)

    scale = (h / 1.72).astype(np.float32)
    ca, sa = np.cos(ang).astype(np.float32), np.sin(ang).astype(np.float32)
    for up in (False, True):
        sel = arms_up == up
        k = int(sel.sum())
        if not k:
            continue
        tp, tn, part, tidx = person_template(up)
        lx, ly, lz = (tp[None, :, i] * scale[sel, None] for i in range(3))
        c, sn = ca[sel, None], sa[sel, None]
        pos = np.empty((k, len(tp), 3), np.float32)
        pos[..., 0] = base[sel, 0, None] + lx * c - ly * sn
        pos[..., 1] = base[sel, 1, None] + lx * sn + ly * c
        pos[..., 2] = base[sel, 2, None] + lz
        nrm = np.empty_like(pos)
        nrm[..., 0] = tn[None, :, 0] * c - tn[None, :, 1] * sn
        nrm[..., 1] = tn[None, :, 0] * sn + tn[None, :, 1] * c
        nrm[..., 2] = np.broadcast_to(tn[None, :, 2], (k, len(tp)))
        cols = palette[sel][:, part]                                        # (k, V, 3)
        uv = np.stack([np.repeat(phase[sel][:, None], len(tp), 1), np.ones((k, len(tp)), np.float32)], -1)
        idx = (np.arange(k)[:, None] * len(tp) + tidx.ravel()[None]).ravel()
        m.add(pos.reshape(-1, 3), nrm.reshape(-1, 3), cols.reshape(-1, 3), PLAIN, uv=uv.reshape(-1, 2), idx=idx, style=CROWD)
    stats["spectators"] = int(n)
    return int(n)


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


def quay_walls(m, sea, mnt, stats):
    """Port Hercule's quays end in a vertical stone wall at the water, not
    in a slope: the 3 m ground grid ramps from the quay down to the sea
    floor and reads as a beach from the car. Along the harbour, a 4 m band
    of quay is laid as a flat stone slab at the quay's own height, with
    its face dropping straight into the water and a pale coping on top."""
    if TRACK is None:
        return 0
    t = TRACK
    n = len(t["x"])
    step = float(np.median(np.hypot(np.diff(t["x"]), np.diff(t["y"]))))
    s_ = np.arange(n) * step
    tb = TUNNEL["tb"] if TUNNEL is not None else -1
    sel = (t["z"] < 8.0) & (s_ > tb + 30)
    if sel.sum() < 10:
        return 0
    zone = LineString(np.column_stack([t["x"][sel], t["y"][sel]])).buffer(160)
    land = ~sea
    dist = ndimage.distance_transform_edt(land) * RES
    band = land & (dist <= 4.0)
    # Quay height: the land's own height just inland, spread to the edge.
    m_ = np.where(land & (dist > 4.5) & (dist < 9) & np.isfinite(mnt), mnt, np.nan)
    idx = ndimage.distance_transform_edt(np.isnan(m_), return_distances=False, return_indices=True)
    qz = ndimage.median_filter(m_[idx[0], idx[1]], size=9)
    qR = Raster(qz.astype(np.float32))
    seaR = Raster(sea.astype(np.float32))
    rr, cc = np.nonzero(band)
    xs, ys = cell_xy(rr, cc)
    inz = contains_xy(zone, xs, ys)
    bm = np.zeros_like(band)
    bm[rr[inz], cc[inz]] = True
    lab, nl = ndimage.label(bm)
    stone, coping, wall = np.array(hexrgb("#d3cdc1")), np.array(hexrgb("#ece7db")), np.array(hexrgb("#a79f90"))
    keep = TRACK["line"].buffer(TRACK_HALF + 1.0)
    total = 0.0
    for j, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        sub = lab[sl] == j
        if sub.sum() < 40:
            continue
        x0 = X0 + sl[1].start * RES
        y1 = Y1 - sl[0].start * RES
        poly = cells_to_poly(sub, x0, y1, RES)
        poly = poly.difference(keep)
        for pg in to_polys(poly):
            if pg.area < 8:
                continue
            pg = orient(pg.simplify(0.3), 1.0)
            tr = triangulate(pg, area=6)
            if tr is None:
                continue
            v, tri = tr
            z = qR.sample(v[:, 0], v[:, 1])
            z = np.where(np.isfinite(z), z, 1.5) + 0.06
            P = enu_fast(v[:, 0], v[:, 1], z)
            m.tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], stone, PLAIN)
            # The wall: every ring edge with water just outside it.
            ring = np.asarray(pg.exterior.coords)[:, :2]
            a, b = ring[:-1], ring[1:]
            L = np.linalg.norm(b - a, axis=1)
            ok = L > 0.05
            a, b, L = a[ok], b[ok], L[ok]
            u = (b - a) / L[:, None]
            out = np.column_stack([u[:, 1], -u[:, 0]])
            mid = (a + b) / 2
            wet = seaR.sample(mid[:, 0] + out[:, 0] * 1.2, mid[:, 1] + out[:, 1] * 1.2) > 0.5
            if not wet.any():
                continue
            a, b, out = a[wet], b[wet], out[wet]
            za = np.nan_to_num(qR.sample(a[:, 0], a[:, 1]), nan=1.5) + 0.06
            zb = np.nan_to_num(qR.sample(b[:, 0], b[:, 1]), nan=1.5) + 0.06
            k = len(a)
            A0 = enu_fast(a[:, 0], a[:, 1], np.full(k, -1.5)); B0 = enu_fast(b[:, 0], b[:, 1], np.full(k, -1.5))
            A1 = enu_fast(a[:, 0], a[:, 1], za); B1 = enu_fast(b[:, 0], b[:, 1], zb)
            m.tri(A0, B0, B1, wall, PLAIN); m.tri(A0, B1, A1, wall, PLAIN)
            # Coping: a 0.7 m pale stone strip along the edge, a hair higher.
            ai, bi = a - out * 0.7, b - out * 0.7
            C0 = enu_fast(a[:, 0], a[:, 1], za + 0.04); C1 = enu_fast(b[:, 0], b[:, 1], zb + 0.04)
            D0 = enu_fast(ai[:, 0], ai[:, 1], za + 0.04); D1 = enu_fast(bi[:, 0], bi[:, 1], zb + 0.04)
            m.tri(C0, C1, D1, coping, PLAIN); m.tri(C0, D1, D0, coping, PLAIN)
            total += float(np.linalg.norm(b - a, axis=1).sum())
    stats["quay_wall_m"] = int(total)
    return int(total)


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
            add_walls(m, p, -0.5, 0.9, -0.5, 10, hexrgb("#8f8b82"), PLAIN)
            add_roof(m, p, 0.9, hexrgb("#cfcac0"), False)


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
    # UV: the district's map space (as the ground map), for the shore
    # foam in the night/shore texture; outside the district it clamps.
    Ainv = np.linalg.lstsq(np.column_stack([AFF[0, :2], AFF[1, :2]]).T, np.eye(2), rcond=None)[0]
    lxy = (P[:, :2] - AFF[2, :2]) @ Ainv
    uv = np.column_stack([lxy[:, 0] / (X1 - X0), 1 - lxy[:, 1] / (Y1 - Y0)])
    m.add(P, [0, 0, 1], hexrgb("#2d7a92"), WATER, uv=uv, idx=idx)


# --------------------------------------------------------------------- glb ----
def write_glb(path, prims, ground_jpg, roof_jpg=None):
    """prims: list of (Mesh, textured): textured False, True (the ground
    map) or "roof" (the roof atlas). Positions are ENU; glTF is y-up."""
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
    images = [{"bufferView": tex_view, "mimeType": "image/jpeg"}]
    if roof_jpg is not None:
        materials.append({"pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1], "baseColorTexture": {"index": 1},
                                                   "metallicFactor": 0, "roughnessFactor": 0.8}})
        images.append({"bufferView": add_view(roof_jpg), "mimeType": "image/jpeg"})
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
            # Every primitive has a second set (zeros except on the road
            # surfaces), so the viewer's one shader can read it everywhere.
            "TEXCOORD_1": acc(np.concatenate(mesh.uv2).astype("<f4"), 5126, "VEC2", 34962),
        }
        idx = acc(i.astype("<u4"), 5125, "SCALAR", 34963)
        mat = 2 if textured == "roof" and roof_jpg is not None else (1 if textured else 0)
        meshes_p.append({"attributes": attrs, "indices": idx, "material": mat})
    gltf = {
        "asset": {"version": "2.0", "generator": "MC-3D build_filmset.py"},
        # One mesh (and node) per primitive: the compressor quantises each
        # mesh within its own bounds, and the 40 km sea plane must not share
        # them with the district (16 bits over 40 km is 60 cm steps, enough
        # to lift the ground through the road).
        "scene": 0, "scenes": [{"nodes": list(range(len(meshes_p)))}],
        "nodes": [{"mesh": k} for k in range(len(meshes_p))],
        "meshes": [{"primitives": [q]} for q in meshes_p],
        "materials": materials,
        "images": images,
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}],
        "textures": [{"source": k, "sampler": 0} for k in range(len(images))],
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


# ------------------------------------------------------------------ roads ----
# Road surfaces of their own over the painted ground map: a strip draped on
# the terrain for every street, with its across/along position in metres
# (TEXCOORD_1) so the viewer draws asphalt grain, patches, tyre wear and
# crisp lane markings and zebra crossings at any distance, where the 37 cm
# ground map could only give a soft grey band. Stone kerbs along both sides.
ROAD_W = {"motorway": 12, "trunk": 11, "primary": 10, "secondary": 8.5, "tertiary": 7.5,
          "residential": 6, "unclassified": 6, "service": 4.5, "living_street": 5,
          "primary_link": 6, "secondary_link": 6, "tertiary_link": 5, "raceway": 9}
ROAD_RANK = {"raceway": 9, "motorway": 8, "trunk": 7, "primary": 6, "secondary": 5, "tertiary": 4,
             "primary_link": 4, "secondary_link": 4, "tertiary_link": 3, "residential": 2,
             "unclassified": 2, "living_street": 1, "service": 0}
# Marking styles (low bits of the colour alpha).
MK_NONE, MK_CENTRE, MK_CENTRE_EDGE, MK_LANES_EDGE, MK_EDGE = range(5)
ROAD_LIFT = 0.06                     # m above the ground triangles
ROAD_SPAN_U, ROAD_SPAN_V = 32.0, 72.0  # TEXCOORD_1 = (across + 16, along) / span


def ground_z(gz, x, y):
    """Height of the ground mesh's own triangles (same diagonal) at x, y."""
    fj = (np.asarray(x) - X0) / GRID
    fi = (Y1 - np.asarray(y)) / GRID
    n = gz.shape[0] - 1
    j0 = np.clip(np.floor(fj).astype(int), 0, n - 1)
    i0 = np.clip(np.floor(fi).astype(int), 0, n - 1)
    fx, fy = np.clip(fj - j0, 0, 1), np.clip(fi - i0, 0, 1)
    a, b = gz[i0, j0], gz[i0, j0 + 1]
    c, d = gz[i0 + 1, j0], gz[i0 + 1, j0 + 1]
    lo = a + fx * (b - a) + fy * (c - a)
    hi = d + (1 - fx) * (c - d) + (1 - fy) * (b - d)
    return np.where(fx + fy <= 1, lo, hi)


def road_surfaces(m, ways, pts, gz, bmask, stats):
    from scipy.spatial import cKDTree
    roads = []
    for t, p_, closed in ways:
        hw = t.get("highway")
        if hw not in ROAD_W or t.get("tunnel") in ("yes", "building_passage") or t.get("area") == "yes" \
                or t.get("layer", "0") not in ("0", "1") or t.get("bridge") or len(p_) < 2:
            continue
        if t.get("layer", "0") == "1" or is_carpark_way(p_):
            continue                    # (car park entrances: a driveway across the pavement)
        W = float(ROAD_W[hw])
        try:
            if t.get("width"):
                W = float(t["width"].replace("m", "").strip())
            elif t.get("lanes"):
                W = max(W, 3.1 * float(t["lanes"]))
        except ValueError:
            pass
        W = float(np.clip(W, 3.0, 20.0))
        if TRACK is not None:
            # The circuit's own streets reach the pavements' kerbs (no strip
            # of bare ground between the tarmac and the kerb).
            q_ = np.asarray(p_, dtype=float)
            med_ = float(np.median(track_dist(q_[:, 0], q_[:, 1])))
            if med_ < 2.5:
                W = max(W, 2 * (SW_START + 0.3 + med_))
        roads.append((t, LineString(p_), W))
    # Junctions: points shared by two roads (markings stop there).
    cnt = {}
    for t, ln, W in roads:
        for q in {(round(x, 2), round(y, 2)) for x, y in ln.coords}:
            cnt[q] = cnt.get(q, 0) + 1
    jpts = np.array([q for q, k in cnt.items() if k >= 2]) if cnt else np.zeros((0, 2))
    jkd = cKDTree(jpts) if len(jpts) else None
    cross = np.array([xy for xy, tg in pts if tg.get("highway") == "crossing"
                      and tg.get("crossing") not in ("unmarked", "no", "informal")
                      and tg.get("crossing:markings") not in ("no",)]) if pts else np.zeros((0, 2))
    ckd = cKDTree(cross) if len(cross) else None
    bR = Raster(bmask.astype(np.float32))
    kerb_top = np.array(hexrgb("#c9c2b4"))
    kerb_face = np.array(hexrgb("#b3ab9c"))
    total, kerb_m = 0.0, 0.0
    for t, ln, W in roads:
        hw = t["highway"]
        half = W / 2
        L = ln.length
        if L < 2:
            continue
        oneway = t.get("oneway") in ("yes", "1", "-1") or t.get("junction") == "roundabout"
        lanes = int(float(t.get("lanes", 0) or 0)) if str(t.get("lanes", "")).replace(".", "").isdigit() else 0
        if hw == "raceway":
            style = MK_EDGE
        elif hw in ("primary", "secondary", "trunk", "motorway", "tertiary") or hw.endswith("_link"):
            style = MK_LANES_EDGE if oneway and (lanes >= 2 or W >= 7) else (MK_EDGE if oneway else MK_CENTRE_EDGE)
        elif hw in ("residential", "unclassified"):
            style = MK_NONE if oneway or W < 5.5 else MK_CENTRE
        else:
            style = MK_NONE
        # Along-road samples: every 1.5 m, plus the edges of each zebra.
        ss = list(np.linspace(0, L, max(2, int(math.ceil(L / 2.0)) + 1)))
        spans = []
        if ckd is not None:
            for k in ckd.query_ball_point(ln.centroid.coords[0], L / 2 + 5):
                cp = Point(cross[k])
                if ln.distance(cp) < 0.6:
                    sc = ln.project(cp)
                    spans.append((sc - 1.6, sc + 1.6))
                    ss += [sc - 1.62, sc - 1.6, sc + 1.6, sc + 1.62]
        ss += list(np.arange(ROAD_SPAN_V, L, ROAD_SPAN_V))
        ss = np.unique(np.clip(ss, 0, L))
        P = np.array([ln.interpolate(v).coords[0] for v in ss])
        tx = np.gradient(P[:, 0], ss, edge_order=1) if len(ss) > 2 else np.full(len(ss), P[-1, 0] - P[0, 0])
        ty = np.gradient(P[:, 1], ss, edge_order=1) if len(ss) > 2 else np.full(len(ss), P[-1, 1] - P[0, 1])
        tl = np.hypot(tx, ty) + 1e-9
        nx_, ny_ = -ty / tl, tx / tl
        offs = np.array([-half, -half / 2, 0.0, half / 2, half]) if W > 6.5 else np.array([-half, 0.0, half])
        X = P[:, 0, None] + nx_[:, None] * offs
        Y = P[:, 1, None] + ny_[:, None] * offs
        Z = ground_z(gz, X, Y)
        if TRACK is not None and float(np.median(track_dist(P[:, 0], P[:, 1]))) > 2.5:
            # Side streets level across (the 3 m grid's humps beside them,
            # walls and banks, had tilted their edges into dark ramps).
            zc = Z[:, len(offs) // 2:len(offs) // 2 + 1]
            Z = np.clip(Z, zc - 0.15, zc + 0.15)
        # Not over the water (a road ending on a quay edge).
        wet = (Z <= SEA_FLOOR + 0.5).any(1)
        Z = Z + ROAD_LIFT + 0.004 * ROAD_RANK.get(hw, 0)
        if TRACK is not None:
            # On and beside the circuit, stay under the tours' modelled road.
            dk, jk = TRACK["kd"].query(np.column_stack([X.ravel(), Y.ravel()]))
            # (A little lower towards the edges: on a climb the nearest lap
            # point's height is not quite the road's beside it.)
            zt = np.where(dk < TRACK_HALF + 4.0, TRACK["z"][jk] - 0.06 - 0.08 * np.clip(dk - 3.0, 0, 1),
                          np.inf).reshape(Z.shape)
            Z = np.minimum(Z, zt)
            # On the circuit itself the road is the lap's own surface, not
            # the 3 m ground grid's: where the ground dips (another leg of
            # the lap passing below) the road kept its level and the
            # ground's slopes no longer show through it.
            onl = (dk < TRACK_HALF + 0.5).reshape(Z.shape)   # (4 cm lower: under the track audit)
            Z = np.where(onl, zt - 0.04, Z)
        jd = jkd.query(P)[0] if jkd is not None else np.full(len(P), 99.0)
        jf = np.clip((jd - (half + 2.5)) / 3.0, 0, 1)
        if hw == "raceway":
            jf[:] = 1.0
        zc = np.zeros(len(ss))
        for a_, b_ in spans:
            zc[(ss >= a_ - 1e-6) & (ss <= b_ + 1e-6)] = 1.0
        # Strips of 72 m (along resets): crisp TEXCOORD_1 after compression.
        # Each break is a sample of its own, shared by both strips.
        for k in range(int(L // ROAD_SPAN_V) + 1):
            base_s = k * ROAD_SPAN_V
            idx = np.nonzero((ss >= base_s - 1e-6) & (ss <= base_s + ROAD_SPAN_V + 1e-6))[0]
            if len(idx) < 2:
                continue
            xx, yy, zz = X[idx].ravel(), Y[idx].ravel(), Z[idx].ravel()
            pos = enu_fast(xx, yy, zz)
            k_ = len(offs)
            u2 = np.tile((offs + ROAD_SPAN_U / 2) / ROAD_SPAN_U, len(idx))
            v2 = np.repeat((ss[idx] - base_s) / ROAD_SPAN_V, k_)
            uv = np.column_stack([(xx - X0) / (X1 - X0), (Y1 - yy) / (Y1 - Y0)])
            col = np.column_stack([np.repeat(jf[idx], k_) * 255, np.repeat(zc[idx], k_) * 255,
                                   np.full(len(xx), min(255, half * 10))])
            tri = []
            for r_ in range(len(idx) - 1):
                if wet[idx[r_]] or wet[idx[r_ + 1]]:
                    continue
                for q in range(k_ - 1):
                    a0, b0 = r_ * k_ + q, r_ * k_ + q + 1
                    a1, b1 = a0 + k_, b0 + k_
                    tri += [(a0, a1, b0), (b0, a1, b1)]
            if not tri:
                continue
            tri = np.array(tri)
            # Front faces up.
            pa, pb, pc = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
            up = np.cross(pb - pa, pc - pa)[:, 2] < 0
            tri[up] = tri[up][:, [0, 2, 1]]
            nrm = np.tile([0.0, 0.0, 1.0], (len(pos), 1))
            m.add(pos, nrm, col, ROAD, uv=uv, idx=tri, style=style, uv2=np.column_stack([u2, v2]))
        total += L
        # Kerbs: a 25 cm stone kerb, 13 cm proud of the road, on both sides,
        # away from junctions, the circuit (its own pavements), buildings
        # and lanes without pavements.
        if hw in ("service", "raceway", "living_street"):
            continue
        for side in (-1, 1):
            o0, o1 = side * half, side * (half + 0.25)
            ok = (jf > 0.99) & ~wet
            ex, ey = P[:, 0] + nx_ * o1, P[:, 1] + ny_ * o1
            ok &= bR.sample(ex, ey) < 0.5
            if TRACK is not None:
                ok &= track_dist(P[:, 0], P[:, 1]) > TRACK_HALF + 6
            lab, nr = ndimage.label(ok)
            for r_ in range(1, nr + 1):
                ii = np.nonzero(lab == r_)[0]
                if len(ii) < 3:
                    continue
                xa, ya = P[ii, 0] + nx_[ii] * o0, P[ii, 1] + ny_[ii] * o0
                xb, yb = P[ii, 0] + nx_[ii] * o1, P[ii, 1] + ny_[ii] * o1
                za = ground_z(gz, xa, ya) + ROAD_LIFT
                zb_ = ground_z(gz, xb, yb)
                ztop = np.maximum(za, zb_) + 0.13
                A0 = enu_fast(xa, ya, za - 0.05); A1 = enu_fast(xa, ya, ztop)
                B1 = enu_fast(xb, yb, ztop); B0 = enu_fast(xb, yb, zb_ - 0.1)
                cin = enu_fast(P[ii, 0], P[ii, 1], za + 0.5)          # toward the road
                cout = enu_fast(P[ii, 0] + nx_[ii] * side * (half + 3), P[ii, 1] + ny_[ii] * side * (half + 3), za + 0.5)
                sl = slice(0, -1); sr = slice(1, None)
                tri_facing(m, A0[sl], A0[sr], A1[sr], cin[sl], kerb_face, PLAIN)
                tri_facing(m, A0[sl], A1[sr], A1[sl], cin[sl], kerb_face, PLAIN)
                upv = A1 + np.array([0, 0, 10.0])
                tri_facing(m, A1[sl], A1[sr], B1[sr], upv[sl], kerb_top, PLAIN)
                tri_facing(m, A1[sl], B1[sr], B1[sl], upv[sl], kerb_top, PLAIN)
                tri_facing(m, B1[sl], B1[sr], B0[sr], cout[sl], kerb_face, PLAIN)
                tri_facing(m, B1[sl], B0[sr], B0[sl], cout[sl], kerb_face, PLAIN)
                kerb_m += float(ss[ii[-1]] - ss[ii[0]])
    stats["road_m"] = int(total)
    stats["kerb_m"] = int(kerb_m)
    return int(total)


# ---------------------------------------------------------- street lamps ----
# Lamp posts along the streets, and a light map of the pools of light they
# throw on the ground, for the viewer's night mode.
LAMP_HEAD = 3                       # PLAIN style: glows at night only
LAMP_ROADS = {"primary": 10, "secondary": 8.5, "tertiary": 7.5, "residential": 6, "unclassified": 6,
              "living_street": 5, "primary_link": 6, "secondary_link": 6, "tertiary_link": 5, "pedestrian": 4}


def street_lamps(m, ways, gz, bmask, sea):
    """Lamps along the streets; writes monte-carlo-night.jpg: R = pools of
    lamp light, G = distance from the shore over the sea (0-20 m), for the
    foam along quays and beaches."""
    def ground(x, y):
        return float(ndimage.map_coordinates(gz, [[(Y1 - y) / GRID], [(x - X0) / GRID]], order=1, mode="nearest")[0])
    placed = []
    post_c, head_c = np.array(hexrgb("#2d3134")), np.array(hexrgb("#fff1c8"))
    for t, pts, closed in ways:
        w = LAMP_ROADS.get(t.get("highway"))
        if not w or t.get("tunnel") in ("yes", "building_passage") or t.get("area") == "yes" \
                or t.get("layer", "0") not in ("0", "1") or t.get("bridge"):
            continue
        ls = LineString(pts)
        side = 1
        for d_ in np.arange(8, ls.length - 4, 24.0):
            a, b = ls.interpolate(d_), ls.interpolate(min(d_ + 1, ls.length))
            dx, dy = b.x - a.x, b.y - a.y
            l = math.hypot(dx, dy) or 1
            nx_, ny_ = -dy / l * side, dx / l * side
            off = w / 2 + 0.9
            x, y = a.x + nx_ * off, a.y + ny_ * off
            side = -side
            if not (X0 + 2 < x < X1 - 2 and Y0 + 2 < y < Y1 - 2):
                continue
            r, c = int((Y1 - y) / RES), int((x - X0) / RES)
            if bmask[r, c] or sea[r, c]:
                continue
            if TUNNEL is not None and TUNNEL["ground"].contains(shapely.Point(x, y)):
                continue
            if QA is not None and QA["drop"] is not None and QA["drop"].distance(shapely.Point(x, y)) < 2.0:
                continue                                    # (not half inside the Quai Albert wall)
            if not off_track(x, y, -0.5):
                continue
            if any(abs(px - x) < 12 and abs(py - y) < 12 for px, py in placed[-60:]):
                continue
            placed.append((x, y))
            z = ground(x, y)
            # The post (add_walls() drops edges under 30 cm unless told:
            # the lamps had lost their posts and their heads floated).
            add_walls(m, box(x - 0.1, y - 0.1, x + 0.1, y + 0.1), z - 0.3, z + 5.4, z, 10, post_c, PLAIN, min_edge=0.05)
            add_walls(m, box(x - 0.2, y - 0.2, x + 0.2, y + 0.2), z - 0.3, z + 0.9, z, 10, post_c, PLAIN)
            add_roof(m, box(x - 0.2, y - 0.2, x + 0.2, y + 0.2), z + 0.9, post_c, False)
            hx, hy = x - nx_ * 0.7, y - ny_ * 0.7          # the head leans over the road
            if not off_track(hx, hy):
                placed.pop()                                # (never over the circuit)
                continue
            arm = LineString([(x, y), (hx, hy)]).buffer(0.06, cap_style=2)
            add_walls(m, orient(arm, 1.0), z + 5.28, z + 5.4, z, 10, post_c, PLAIN)
            head = box(hx - 0.24, hy - 0.24, hx + 0.24, hy + 0.24)
            add_walls(m, head, z + 5.15, z + 5.35, z, 10, head_c, PLAIN, style=LAMP_HEAD)
            add_roof(m, head, z + 5.35, post_c, False)
            placed[-1] = (hx, hy)
    # Light map: soft pools under each lamp, 2048 px over the district.
    S = 2048
    g = np.zeros((S, S), np.float32)
    for x, y in placed:
        g[int((Y1 - y) / (Y1 - Y0) * S), int((x - X0) / (X1 - X0) * S)] += 1
    g = ndimage.gaussian_filter(g, 5.5 / ((X1 - X0) / S))
    g = np.clip(g / g.max() * 1.4, 0, 1) ** 0.8
    seaS = np.asarray(Image.fromarray((sea * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)) > 127
    dist = ndimage.distance_transform_edt(seaS) * ((X1 - X0) / S)
    shore = np.clip(dist / 20.0, 0, 1)
    rgb = np.dstack([g, shore, np.zeros_like(g)])
    im = Image.fromarray((rgb * 255).astype(np.uint8), "RGB")
    im.save(OUT / "monte-carlo-night.jpg", quality=88, subsampling=0)
    return len(placed)


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
    ortho = np.array(Image.open(RAW / "ortho.jpg").convert("RGB"))
    ortho_hi = load_ortho_hi()
    # Sea: no terrain in the LiDAR, or ground at sea level.
    # Moored yachts and pontoons show up as small islands of "ground" in the
    # LiDAR terrain model; anything smaller than 0.2 ha surrounded by water
    # is sea (yachts are rebuilt below, OSM piers become pontoons).
    sea = np.isnan(mnt) | (mnt < 0.6)
    lab, n = ndimage.label(~sea)
    sizes = ndimage.sum(~sea, lab, np.arange(1, n + 1))
    sea |= np.isin(lab, np.nonzero(sizes < 8000)[0] + 1)
    sea = ndimage.binary_opening(sea, iterations=2)
    # And the other way round: dark cars and puddles on the low quays
    # (the car park on the harbour's south quay is at 0.5 m) read as "sea" holes in the
    # land, and the yacht tracer put a boat in each white van parked
    # there. A patch of "sea" under 0.2 ha that does not reach the open
    # water is quay.
    lab, n = ndimage.label(sea)
    sizes = ndimage.sum(sea, lab, np.arange(1, n + 1))
    sea &= ~np.isin(lab, np.nonzero(sizes < 8000)[0] + 1)
    ways, pts = load_osm()
    global SHOPS
    SHOPS = shop_places(ways, pts)
    print("real shops:", len(SHOPS["pts"]), "points,", len(SHOPS["areas"]), "areas")
    # Yachts moored against a quay merge with it in the LiDAR terrain. The
    # OpenStreetMap coastline follows the quays, so anything outside it is sea.
    land = coast_land(ways, sea)
    sea |= ~land
    global MT
    MT = mareterra_setup(ways, mnt, mns, ortho, sea, land)
    mnt_f = np.where(sea, np.nan, mnt)
    global TUNNEL
    TUNNEL = load_tunnel()
    if TUNNEL is not None:
        tunnel_open(sea)
        if "open" in TUNNEL:
            print("tunnel open on the sea side from", TUNNEL["open"])
    global TRACK, PROM
    TRACK = load_track()
    if TRACK is not None:
        flat = flatten_harbour(mnt_f)
        flat, port_m2 = flatten_port(flat, sea)
        print("port humps levelled:", port_m2, "m2")
        # Lower the surface model by as much, so what stands there keeps
        # its height (and the humps do not turn into trees).
        mns = mns - np.nan_to_num(mnt_f - flat, nan=0.0)
        mnt_f = flat
        PROM = ostende_setup(mnt_f)
        global QA
        mnt_before = mnt_f.copy()
        QA = quai_setup(ways, mnt_f, sea)
        if QA is not None:
            # The pit buildings and stands on the esplanade come off the
            # surface model too (no shadows, no "trees"); the pines stay.
            o_ = ortho.astype(np.float32)
            green = (2 * o_[..., 1] - o_[..., 0] - o_[..., 2]) > 10
            chg = np.isfinite(mnt_f) & (np.nan_to_num(mnt_f) != np.nan_to_num(mnt_before))
            mns = np.where(chg & ~(green & (mns - mnt_before > 3)), mnt_f, mns)
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
    photo = None
    if ortho_hi is not None:
        edges = photo_edges(ortho_hi)
        lean = estimate_lean(edges, feats, mns, mnt)
        print("photo lean per metre of height (east, north):", lean)
        photo = {"mesh": Mesh(), "polys": [], "uvmap": [], "tiled": [], "lean": lean, "edges": edges}
    facades = FacadeData(ways)
    feats = mareterra_feats(feats, ways)
    global SW
    entrances = carpark_entrances(ways, pts)
    SW = sidewalks_setup(ways, mnt_f, sea, feats)
    if SW is not None and mnt_ground is not mnt_f:
        m_ = raster_mask(SW["poly"])
        mnt_ground[m_] = mnt_f[m_]
    fps = buildings(bm, feats, mns, mnt_f, ortho, stats, facades, photo)
    if MT is not None:
        # Mareterra's new blocks into the surface model, so they cast
        # their baked shadows on the ground map like the rest.
        for f in feats:
            if "mareterra" in f["properties"]:
                mk = raster_mask(shape(f["geometry"]))
                mns[mk] = np.nan_to_num(mnt_f[mk], nan=MT_PLATEAU) + f["properties"]["hauteur"]
    stats["photo_facade_colours"] = facades.used_photo_colours
    stats["photo_facade_types"] = facades.used_survey
    stats["photo_roofs"] = len(photo["polys"]) if photo else 0
    stats["photo_lean"] = photo["lean"] if photo else None
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
    trees(tm, mns, mnt_f, ortho, bgrow.astype(np.uint8), pts, stats, no_tree_zones(ways))
    print("Mareterra planting…", mareterra_trees(tm, ways, mnt_f, bgrow, stats))
    print("harbour…")
    hm = Mesh()
    yachts(hm, ortho, sea, stats, ways)
    piers(hm, ways, sea)
    print("quay walls…", quay_walls(hm, sea, mnt_f, stats))
    stats["bridges"] = bridges(bm, ways, mns, mnt)
    stats["diving_towers"] = pool_features(bm, ways, mnt_f)
    sm = Mesh()
    sea_plane(sm)

    print("ground…")
    chm = np.nan_to_num(mns - mnt_f, nan=0)
    canopy = ((chm > 2.5) & ~bgrow).astype(np.float32)
    canopy = ndimage.zoom(canopy, 0.5, order=1)
    gm, gz = ground_mesh(mnt_ground, sea)
    rm = Mesh()
    print("roads…", road_surfaces(rm, ways, pts, gz, bmask, stats), "m")
    drive = drive_map(gz, bmask, sea)
    stats["lamps"] = street_lamps(bm, ways, gz, bmask, sea)
    print("crowds…", crowds(bm, mnt_f, sea, bmask, stats))
    if SW is None:
        pavements(bm, sea, bmask, stats)
    print("sidewalks…", sidewalks(gm, bm, stats))
    print("street walls…", street_walls(gm, bm, stats))
    print("car park doors…", garage_doors(bm, entrances, fps, stats))
    print("starting grid…", start_grid(bm, stats), "slots")
    ostende_promenade(bm, mnt_f, stats)
    print("street features…", street_features(bm, ways, pts, mnt_f, stats))
    print("Quai Albert 1er…", quai_albert(gm, bm, tm, ways, pts, Raster(mnt_f), stats, fps), "pines")
    jpg = paint_ground(ways, mnt_f, ndimage.zoom(sea.astype(np.float32), 0.5, order=1),
                       ndimage.zoom(bmask.astype(np.float32), 0.5, order=1), canopy, ortho,
                       sun_shadows(harbour_shadow_surface(mns, mnt_f, bgrow), sea), roofs=(ortho_hi, photo["polys"] if photo else None))

    audit = [("ground", gm), ("buildings", bm), ("trees", tm), ("harbour", hm), ("roads", rm)]
    if photo and photo["mesh"].p:
        audit.append(("photo roofs", photo["mesh"]))
    hits = track_audit(audit)
    stats["track_clear"] = not hits
    if hits:
        print(f"track audit: {len(hits)} spots stand on the road")
        rep = {}
        for s, o, h, nm in hits:
            key = (int(s // 10) * 10, nm)
            if key not in rep or h > rep[key][1]:
                rep[key] = (round(o, 1), round(h, 2))
        for (s, nm), (o, h) in sorted(rep.items()):
            print(f"  s {s:5d} m  {nm:18s} offset {o:+5.1f} m  {h:+.2f} m above the road")
    else:
        print("track audit: the road is clear all the way round")
    if tun_m.p:
        tiny = io.BytesIO()
        Image.new("RGB", (4, 4), (128, 128, 128)).save(tiny, "JPEG")
        write_glb(OUT / "monte-carlo-tunnel.glb", [(tun_m, False)], tiny.getvalue())
    raw = OUT / "monte-carlo.raw.glb"
    prims = [(gm, True), (rm, True), (bm, False), (tm, False), (hm, False), (sm, False)]
    roof_jpg = None
    if photo and photo["mesh"].p:
        roof_jpg, stats["roof_texels_per_m"] = roof_atlas(ortho_hi, photo, bm, stats)
        print("roof atlas:", stats["roof_texels_per_m"], "texels per metre")
        prims.append((photo["mesh"], "roof"))
    write_glb(raw, prims, jpg, roof_jpg)
    out = OUT / "monte-carlo.glb"
    if args.no_compress:
        raw.replace(out)
    else:
        # meshopt: 16-bit positions within each mesh's own bounds keep about
        # 2 cm precision over the district (see write_glb).
        subprocess.run(["npx", "--yes", "@gltf-transform/cli@4.1.1", "meshopt", str(raw), str(out),
                        "--quantize-position", "16", "--quantize-texcoord", "14",
                        "--quantize-normal", "8"], check=True)
        raw.unlink()
    tris = sum(len(np.concatenate(x[0].i)) // 3 for x in prims if x[0].i)
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
                    "IGN BD ORTHO 20 cm, roofs (Etalab 2.0)", "OpenStreetMap contributors (ODbL)",
                    "Wikidata (CC0)", "Mapillary contributors (CC-BY-SA 4.0), facade colours"],
    }
    (OUT / "monte-carlo.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta), f"{time.time() - t0:.0f}s", f"{out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
