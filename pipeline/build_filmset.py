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
  Le Rocher  Monaco-Ville: the generic pipeline, plus the Prince's Palace,
             the Oceanographic Museum, the Cathedral and Fort Antoine built
             piece by piece (Frame, palais_princier, musee_oceanographique,
             cathedrale, fort_antoine); the old town's lanes in setts with
             brick pavements, its pale shutters, the ramparts and cannons,
             the gardens' pines and paths (rocher_streets, rocher_ramparts,
             rocher_gardens).
  Fontvieille The reclaimed district south-west of the Rock: the generic
             pipeline, plus the Stade Louis II (stade_louis_ii: its
             LiDAR-shaped pyramid roofs, office façades, raised pitch,
             stands, yellow away end and nine white arches) and the
             Chapiteau's white big top on four masts (chapiteau).

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
import itertools
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
import shapely.affinity
import shapely.prepared
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
# the whole Grand Prix circuit, plus Le Rocher (Monaco-Ville) to the south and
# Fontvieille (Stade Louis II, the Chapiteau, its port) to the south-west.
# Lambert-93 metres (a square). X0 and Y0 move by multiples of the 3 m
# ground grid, or the lap's ground shifts under the road.
X0, Y0, X1, Y1 = 1055100, 6301552, 1057548, 6304000
ORTHO_TILES = 3                     # 20 cm photo: ORTHO_TILES x ORTHO_TILES tiles of 3840 px
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
    bd:serviceParam wikibase:radius "2.4" . }
  OPTIONAL { ?item wdt:P571 ?inc } OPTIONAL { ?item wdt:P149 ?style } OPTIONAL { ?item wdt:P1101 ?floors }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,fr". } }"""


def _get(url, tries=5):
    """GET with retries: big WMS images are sometimes cut off mid-transfer."""
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MC-3D film set builder"})
            return urllib.request.urlopen(req, timeout=300).read()
        except Exception as e:                  # noqa: BLE001 (network)
            if k == tries - 1:
                raise
            print("  retry after", type(e).__name__)
            time.sleep(5 * (k + 1))


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
        # Dated places around the square (year built, style, floors).
        "wikidata.json": "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(WD_QUERY),
    }
    # The roofs, at about 20 cm (the photo's own resolution): tiles of
    # 3840 px, PNG so the WMS's JPEG blocks do not land on the roofs.
    half = (X1 - X0) / ORTHO_TILES
    for i in range(ORTHO_TILES):
        for j in range(ORTHO_TILES):
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
        out.write_bytes(_get(url))
        time.sleep(1.2)
    # OpenStreetMap: the API caps one /map call at 50 000 nodes, so the
    # district comes in four pieces, merged without duplicates.
    out = RAW / "osm.xml"
    if not (out.exists() and out.stat().st_size > 1000):
        merged, seen = ET.Element("osm", version="0.6"), set()
        lons, lats = np.linspace(lon0[0], lon1[0], 3), np.linspace(lat0[0], lat1[0], 3)
        for i in range(2):
            for j in range(2):
                url = ("https://api.openstreetmap.org/api/0.6/map?bbox="
                       f"{lons[i]:.4f},{lats[j]:.4f},{lons[i + 1]:.4f},{lats[j + 1]:.4f}")
                print("fetch osm.xml", i, j)
                root = ET.fromstring(_get(url))
                for el in root:
                    if el.tag in ("node", "way", "relation") and (el.tag, el.get("id")) not in seen:
                        seen.add((el.tag, el.get("id")))
                        merged.append(el)
                time.sleep(1.2)
        # Nodes first, then ways, then relations, as the API writes them.
        merged[:] = sorted(merged, key=lambda e: ("node", "way", "relation").index(e.tag))
        ET.ElementTree(merged).write(out, encoding="utf-8", xml_declaration=True)


def load_ortho_hi():
    """The 20 cm photo tiles as one image (north up), or None."""
    N = ORTHO_TILES
    tiles = [[RAW / f"ortho20_{i}{j}.png" for j in range(N)] for i in range(N)]
    if not all(t.exists() for row in tiles for t in row):
        return None
    k = Image.open(tiles[0][0]).size[0]
    im = Image.new("RGB", (N * k, N * k))
    for i in range(N):
        for j in range(N):
            im.paste(Image.open(tiles[i][j]).convert("RGB"), (k * i, k * (N - 1 - j)))
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
    if FM is not None:
        # The Fairmont's sunken roof courts: flat at their floor.
        for court_, zf in zip(FM["courts"], FM["floors"]):
            inc = contains_xy(court_, X, Y)
            Z[inc] = zf - 0.3
        if "rim" in FM:
            inr = contains_xy(FM["rim"], X, Y)
            Z[inr] = np.minimum(Z[inr], FM["rim_z"] - 0.2)
    Z, Zs = quai_ground(X, Y, Z)
    Z, Zs = sidewalk_ground(X, Y, Z, Zs)
    Z = rampe_clamp(X, Y, Z)
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
    for pt_ in PORTALS:
        # A bridge running into a tunnel: the ground grid's slope from the
        # hill above the mouth down to the ground under the deck would
        # hang across the opening. portals() fills the hole.
        cut = contains_xy(pt_["cut"], X, Y)
        hole = cut[:-1, :-1] | cut[:-1, 1:] | cut[1:, :-1] | cut[1:, 1:]
        keep &= ~hole
        ii_, jj_ = np.nonzero(hole)
        pt_["hole"] = unary_union([box(X[i, j], Y[i, j] - GRID, X[i, j] + GRID, Y[i, j]) for i, j in zip(ii_, jj_)])
    global GROUND_Z
    GROUND_Z = Z
    if FM is not None and "cut" in FM:
        # Round the Fairmont's open court the wall, the court's floor and
        # the rim's slab stand in for the ground grid.
        cut = contains_xy(FM["cut"], X, Y)
        keep &= ~(cut[:-1, :-1] | cut[:-1, 1:] | cut[1:, :-1] | cut[1:, 1:])
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
    # The cliffs all round Le Rocher: a 1 m grid over every cell with steep
    # LiDAR ground in it (and a margin), so the faces stand up as rock and
    # do not slump into 3 m ramps.
    M = rocher_fine_cells(mnt, Z.shape)
    if M is not None:
        M &= keep
        keep &= ~M
        fine.append(_detail_mask_patch(mnt, Z, M))
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
    Zf = rampe_clamp(Xf, Yf, Zf)
    P = enu_fast(Xf.ravel(), Yf.ravel(), Zf.ravel())
    gy, gx = np.gradient(Zf, st)
    N = np.column_stack([-gx.ravel(), gy.ravel(), np.ones(Zf.size)])
    N /= np.linalg.norm(N, axis=1, keepdims=True)
    uv = np.column_stack([(Xf.ravel() - X0) / (X1 - X0), (Y1 - Yf.ravel()) / (Y1 - Y0)])
    ii = np.arange(nxf * nyf).reshape(nyf, nxf)
    a, b, cc, d = ii[:-1, :-1], ii[:-1, 1:], ii[1:, :-1], ii[1:, 1:]
    tris = np.concatenate([np.stack([a, cc, b], -1).reshape(-1, 3), np.stack([b, cc, d], -1).reshape(-1, 3)])
    return P, N, uv, tris


ROCHER_FINE_BOX = (1055890.0, 6302230.0, 1056720.0, 6302700.0)


def rocher_fine_cells(mnt, shape):
    """Coarse ground cells round Le Rocher that get the 1 m grid: any cell
    with LiDAR ground steeper than ~42 degrees, grown by 6 m."""
    x0, y0, x1, y1 = ROCHER_FINE_BOX
    n = shape[0] - 1
    k = int(round(GRID / RES))
    j0, j1 = int((x0 - X0) // GRID), int((x1 - X0) // GRID)
    i0, i1 = int((Y1 - y1) // GRID), int((Y1 - y0) // GRID)
    if j1 <= j0 or i1 <= i0:
        return None
    sub = mnt[i0 * k:i1 * k, j0 * k:j1 * k]
    if sub.shape != ((i1 - i0) * k, (j1 - j0) * k):
        return None
    gy, gx = np.gradient(np.nan_to_num(sub, nan=0.0), RES)
    st = (np.hypot(gx, gy) > 0.9) & np.isfinite(sub) & (sub > 0.6)
    cell = st.reshape(i1 - i0, k, j1 - j0, k).sum((1, 3)) >= 2
    cell = ndimage.binary_dilation(cell, iterations=2)
    M = np.zeros((n, n), bool)
    M[i0:i1, j0:j1] = cell
    if TRACK is not None:
        # Never under the circuit (the track rules own those heights).
        ci, cj = np.nonzero(M)
        cx_, cy_ = X0 + (cj + 0.5) * GRID, Y1 - (ci + 0.5) * GRID
        far = track_dist(cx_, cy_) > TRACK_HALF + 12
        M[ci[~far], cj[~far]] = False
    return M


def _detail_mask_patch(mnt, Z, M):
    """Like _detail_patch, over any set of coarse cells M: one fine grid on
    M's bounding box, keeping only the fine quads inside M (and not sea
    floor). Nodes on M's outline keep the coarse heights (no cracks), the
    LiDAR fades in over 4 m inside."""
    K = DETAIL_K
    st = GRID / K
    ii_, jj_ = np.nonzero(M)
    i0, i1, j0, j1 = ii_.min(), ii_.max() + 1, jj_.min(), jj_.max() + 1
    nxf, nyf = (j1 - j0) * K + 1, (i1 - i0) * K + 1
    xs = X0 + j0 * GRID + np.arange(nxf) * st
    ys = Y1 - i0 * GRID - np.arange(nyf) * st
    Xf, Yf = np.meshgrid(xs, ys)
    fi = (Y1 - Yf) / GRID
    fj = (Xf - X0) / GRID
    ia = np.clip(np.floor(fi).astype(int), 0, Z.shape[0] - 2)
    ja = np.clip(np.floor(fj).astype(int), 0, Z.shape[1] - 2)
    ti, tj = fi - ia, fj - ja
    Zc = (Z[ia, ja] * (1 - ti) * (1 - tj) + Z[ia, ja + 1] * (1 - ti) * tj +
          Z[ia + 1, ja] * ti * (1 - tj) + Z[ia + 1, ja + 1] * ti * tj)
    h = ndimage.uniform_filter(np.nan_to_num(mnt, nan=0.0), size=3)
    w = ndimage.uniform_filter((~np.isnan(mnt)).astype(np.float32), size=3)
    r = np.clip(((Y1 - Yf) / RES).astype(int), 0, mnt.shape[0] - 1)
    c = np.clip(((Xf - X0) / RES).astype(int), 0, mnt.shape[1] - 1)
    Zm = np.where(w[r, c] > 0.3, h[r, c] / np.maximum(w[r, c], 1e-6), np.nan)
    Mf = np.repeat(np.repeat(M[i0:i1, j0:j1], K, 0), K, 1)        # fine quads
    pad = np.pad(Mf, 1)
    inside = pad[:-1, :-1] & pad[:-1, 1:] & pad[1:, :-1] & pad[1:, 1:]   # nodes
    wt = np.clip(ndimage.distance_transform_edt(inside) * st / 4.0 - 0.25, 0.0, 1.0)
    wt = np.where(np.isfinite(Zm) & (Zc > SEA_FLOOR + 1), wt, 0.0)
    Zf = Zc + wt * (np.nan_to_num(Zm, nan=0.0) - Zc)
    Zf = rampe_clamp(Xf, Yf, Zf)
    P = enu_fast(Xf.ravel(), Yf.ravel(), Zf.ravel())
    gy, gx = np.gradient(Zf, st)
    N = np.column_stack([-gx.ravel(), gy.ravel(), np.ones(Zf.size)])
    N /= np.linalg.norm(N, axis=1, keepdims=True)
    uv = np.column_stack([(Xf.ravel() - X0) / (X1 - X0), (Y1 - Yf.ravel()) / (Y1 - Y0)])
    ii = np.arange(nxf * nyf).reshape(nyf, nxf)
    a, b, cc, d = ii[:-1, :-1], ii[:-1, 1:], ii[1:, :-1], ii[1:, 1:]
    low = Zf <= SEA_FLOOR + 0.05
    seaq = low[:-1, :-1] & low[:-1, 1:] & low[1:, :-1] & low[1:, 1:]
    kq = Mf & ~seaq
    tris = np.concatenate([np.stack([a, cc, b], -1)[kq], np.stack([b, cc, d], -1)[kq]])
    used, tris = np.unique(tris, return_inverse=True)
    tris = tris.reshape(-1, 3)
    return P[used], N[used], uv[used], tris


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


# The garden on the Parking des Pêcheurs' roof (east of the Oceanographic
# Museum, 25 m over the sea): outline and beds from the 20 cm photo.
PECHE_DECK = Polygon([(1056497.0, 6302403.0), (1056541.0, 6302393.5), (1056601.0, 6302472.0),
                      (1056607.0, 6302510.0), (1056592.0, 6302514.0), (1056572.0, 6302497.0),
                      (1056547.0, 6302481.0), (1056531.0, 6302463.0), (1056505.0, 6302446.0),
                      (1056495.0, 6302426.0)])
PECHE_EDGE = [(1056515.0, 6302401.0), (1056525.0, 6302400.0), (1056542.0, 6302396.0),
              (1056605.0, 6302474.0), (1056610.0, 6302507.0)]   # the sett walk (OpenStreetMap)
PECHE_ZONES = {
    "earth": [(1056505.0, 6302431.0), (1056512.0, 6302443.0), (1056527.0, 6302457.0), (1056537.0, 6302450.0),
              (1056537.0, 6302438.0), (1056525.0, 6302422.0)],
    "lawn_se": [(1056541.9, 6302420.4), (1056554.4, 6302437.25), (1056561.25, 6302432.25), (1056545.6, 6302412.9)],
    "skate": [(1056531.25, 6302428.5), (1056540.0, 6302439.0), (1056553.75, 6302438.5), (1056543.75, 6302421.0)],
    "turf": [(1056510.0, 6302417.0), (1056516.25, 6302421.6), (1056525.0, 6302415.4), (1056518.75, 6302409.75)],
    "lawn_n": [(1056532.5, 6302458.5), (1056538.75, 6302467.25), (1056543.75, 6302463.5), (1056537.5, 6302454.75)],
    "cinema": [(1056545.0, 6302478.5), (1056556.25, 6302491.0), (1056592.5, 6302468.5), (1056577.5, 6302449.75),
               (1056563.75, 6302452.25)],
}
PECHE_COLS = {"earth": "#a38d6c", "lawn_se": "#5e8446", "skate": "#bdbab3", "turf": "#4b8048",
              "lawn_n": "#5e8446", "cinema": "#d9d3c6"}


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
        if k in GREEN and t.get("name") not in PAVED_GARDENS and not (
                k == "pitch" and PECHE_DECK.intersects(Point(pts[0]))):
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
        # IGN blurs its photo over the Palace and its square into big
        # blocks: no lawn from there (the trees come from the LiDAR).
        veg &= ~raster_mask(Frame(None, PALACE_O, PALACE_U).poly(-60.0, 90.0, -105.0, 65.0))
        vi = Image.fromarray((veg * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR).filter(ImageFilter.GaussianBlur(2))
        vi = ImageChops.multiply(vi.point(lambda v: int(v * 0.85)), keep_green)
        img = Image.composite(Image.new("RGB", (S, S), tuple(hexrgb("#86a65a"))), img, vi)
        d = ImageDraw.Draw(img)
    # Ground under the LiDAR tree canopy: a darker garden green.
    can = Image.fromarray((canopy * 255).astype(np.uint8)).resize((S, S), Image.BILINEAR)
    can = can.filter(ImageFilter.GaussianBlur(3))
    img = Image.composite(Image.new("RGB", (S, S), tuple(hexrgb("#6d8a4c"))), img,
                          ImageChops.multiply(can.point(lambda v: int(min(255, v * 0.85))), keep_green))
    # Le Rocher's cliffs: clean bare limestone all round, over the photo's
    # green smudges and the canopy (the trees on the ledges stay as trees).
    rz = Image.new("L", (S, S), 0)
    bx0, by0, bx1, by1 = ROCHER_FINE_BOX
    ImageDraw.Draw(rz).rectangle([(bx0 - X0) * px, (Y1 - by1) * px, (bx1 - X0) * px, (Y1 - by0) * px], fill=255)
    rz = rz.filter(ImageFilter.GaussianBlur(20))
    ImageDraw.Draw(rz).polygon(xy(np.asarray(PECHE_DECK.buffer(-1.5).exterior.coords)), fill=0)   # the garden's own beds
    rk = ImageChops.multiply(st_.point(lambda v: min(255, int(v * 2.2))), rz)
    img = Image.composite(Image.new("RGB", (S, S), tuple(hexrgb("#b5ab97"))), img, rk)
    d = ImageDraw.Draw(img)
    # Squares and pedestrian areas.
    for t, pts, closed in ways:
        if closed and (t.get("area") == "yes" or t.get("area:highway")) and t.get("highway") in (
                "pedestrian", "footway", "service") or t.get("place") == "square" and closed:
            # Place du Palais is laid in dark red (street photos 2019).
            poly(pts, "#8c5b50" if t.get("name") == "Place du Palais" else "#dfd9cd")
    # South of the Palace's gate the square goes on as a paved terrace under
    # the pines, cannons along its parapet, down to the ramparts' walk: no
    # lawn (the LiDAR pines and the blurred photo painted one there).
    Fp = Frame(None, PALACE_O, PALACE_U)
    fore = Fp.poly(-58.0, 2.0, 0.0, 42.0)
    for t, pts, closed in ways:
        if closed and t.get("leisure") == "garden" and len(pts) >= 3 and Polygon(pts).intersects(fore.buffer(25.0)):
            fore = fore.union(Polygon(pts).buffer(0))
    for q in to_polys(fore):
        poly(np.asarray(q.exterior.coords), "#b8a690")
    for t, pts, closed in ways:
        if closed and t.get("name") == "Place du Palais":
            poly(pts, "#8c5b50")
        if closed and t.get("amenity") == "parking" and t.get("parking") != "underground":
            poly(pts, "#9a9690")
    # The public garden on the roof of the Pêcheurs car park, traced from
    # the 20 cm photo: a pale stone deck, the sett walk round its sea edge,
    # dry earth beds under olive trees, lawns, the small turf pitch, the
    # concrete skate square, the summer cinema's terrace with its rows of
    # seats.
    for q in to_polys(PECHE_DECK):
        poly(np.asarray(q.exterior.coords), "#d2cbbb")
    for name_, pts_ in PECHE_ZONES.items():
        poly(pts_, PECHE_COLS[name_])
    # The cinema's seats: white rows parallel to the screen (north-west side).
    cin = Polygon(PECHE_ZONES["cinema"]).buffer(-1.5)
    u_, n_ = np.array((0.676, 0.737)), np.array((0.737, -0.676))
    c0 = np.array(cin.centroid.coords[0])
    for k in range(-16, 17):
        o = c0 + n_ * k * 0.95
        ln = LineString([o - u_ * 40, o + u_ * 40]).intersection(cin)
        for g_ in getattr(ln, "geoms", [ln]):
            if not g_.is_empty and g_.length > 1:
                line(np.asarray(g_.coords), "#f1efe9", 0.5)
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
    # The west harbours' quays: clean paving, decks, the breakwater's road.
    wq_paint(img, xy, poly, line)
    d = ImageDraw.Draw(img)
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
    # Place du Palais again, over the footway lines drawn across it: its
    # brick and grey setts take their shade from here.
    for t, pts, closed in ways:
        if closed and t.get("name") == "Place du Palais" and t.get("highway") == "pedestrian":
            poly(pts, "#8c5b50")
    for q in to_polys(palais_pattern()):
        poly(np.asarray(q.exterior.coords), "#a19f9a")
    if RAMPE is not None:
        # The Rampe Major's bed: grey stone a little wider than the ribbon.
        for q in to_polys(RAMPE["line"].buffer(RAMPE_W / 2 + 0.5, cap_style=2, join_style=2, mitre_limit=2.0)):
            poly(np.asarray(q.exterior.coords), "#b3aca0")
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
    # The breakwater's abutment: asphalt, walks and the Solarium's decks.
    digue_paint(poly, line)
    if ARMES is not None:
        # Place d'Armes and the Rampe Major in their brick red (the viewer
        # reads the shade against it and lays the bricks).
        for q in to_polys(ARMES["poly"]):
            poly(np.asarray(q.exterior.coords), ARMES_BRICK)
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
    if CONDA is not None:
        # La Condamine's pedestrian streets in pale limestone with their
        # dark sett strip, Square Gastaud in its salmon paving.
        for q in to_polys(CONDA["ped_poly"]):
            poly(np.asarray(q.exterior.coords), SW_PALE)
        for t, ln in CONDA["ped"]:
            line(np.asarray(ln.coords), CONDA_SETT, 1.8)
        if CONDA["park"] is not None:
            for q in to_polys(CONDA["park"]):
                poly(np.asarray(q.exterior.coords), GASTAUD_PAVING)
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
    # Under the park slabs: plain grey, so the map only gives them shade
    # (the viewer draws their grass, grit and beds; see parks()).
    if PARKS is not None and not PARKS["slabs"].is_empty:
        pk_ = Image.new("L", img.size, 0)
        dp_ = ImageDraw.Draw(pk_)
        for q in to_polys(PARKS["slabs"]):
            dp_.polygon(xy(np.asarray(q.exterior.coords)), fill=255)
            for h in q.interiors:
                dp_.polygon(xy(np.asarray(h.coords)), fill=0)
        img.paste(tuple(hexrgb(PARK_GREY)), (0, 0) + img.size, pk_)
        d = ImageDraw.Draw(img)
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
        # On the Pêcheurs garden the LiDAR's trees and kiosks blur into
        # big soft blots over the beds: half strength there.
        dk = Image.new("L", (S, S), 0)
        ImageDraw.Draw(dk).polygon(xy(np.asarray(PECHE_DECK.exterior.coords)), fill=128)
        sh = sh * (1.0 - np.asarray(dk).astype(np.float32) / 255)
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
    arr = fairmont_paint(arr, px, ways, roofs[0] if roofs is not None else None)
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
PALE_NOW = 0.0    # TEXCOORD_1.y on its walls: 1 = white / cream shutters (Le Rocher's old town)

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


ALLEE_FRONT = None   # the Allée François Blanc's line below the square (set in main)


def allee_facing(a, b):
    """Is the wall a-b (CCW, outside on the right) on the Allée François
    Blanc, facing it? Its whole length is a row of boutiques."""
    if ALLEE_FRONT is None:
        return False
    L = float(np.linalg.norm(b - a))
    if L < 1.5:
        return False
    mid = (a + b) / 2
    pl = ALLEE_FRONT.interpolate(ALLEE_FRONT.project(Point(*mid)))
    to = np.array([pl.x, pl.y]) - mid
    d = float(np.linalg.norm(to))
    out_ = np.array([b[1] - a[1], a[0] - b[0]]) / L
    return 1.0 < d < ALLEE_HALF + 2.0 and float(np.dot(out_, to / d)) > 0.6


def shop_spans(a, b):
    """Stretches (fractions 0-1, snapped to whole bays later) of the wall
    a-b with a real shop behind them, from SHOP_NOW."""
    if SHOP_NOW is None:
        return []
    if allee_facing(a, b):
        return [(0.0, 1.0)]
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
        # (The Allée François Blanc climbs 11 m along the Casino: its
        # boutiques follow it all the way up.)
        g = np.where(g > (14.0 if allee_facing(a[i], b[i]) else 6.0), 0.0, np.clip(g, 0.0, None))
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
            pieces = street_rise(a_[okk], b_[okk], base, z0, z1, fh) if ARCADE_NOW is None else None
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
        # Under an arcade (armes_arcade) the wall starts above the arches.
        z0v = np.full(len(a), float(z0))
        if ARCADE_NOW is not None and not inward:
            z0v = np.where(_arcade_mask(a, b), np.maximum(z0v, ARCADE_NOW["z"]), z0v)
            keep = z0v < z1 - 0.01
            a, b, u0, u1, shop, z0v = a[keep], b[keep], u0[keep], u1[keep], shop[keep], z0v[keep]
        k = len(a)
        if not k:
            continue
        P = enu_fast(np.column_stack([a[:, 0], b[:, 0], b[:, 0], a[:, 0]]).ravel(),
                     np.column_stack([a[:, 1], b[:, 1], b[:, 1], a[:, 1]]).ravel(),
                     np.column_stack([z0v, z0v, np.full(k, z1), np.full(k, z1)]).ravel()).reshape(k, 4, 3)
        nrm = np.cross(P[:, 1] - P[:, 0], P[:, 3] - P[:, 0])
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
        v0, v1 = (z0v - base) / fh, np.full(k, (z1 - base) / fh)
        uv = np.stack([np.column_stack([u0, u1, u1, u0]),
                       np.column_stack([v0, v0, v1, v1])], -1)
        idx = (np.arange(k)[:, None] * 4 + np.array([0, 1, 2, 0, 2, 3])[None]).ravel()
        uv2 = np.column_stack([np.repeat(shop, 4), np.full(4 * k, PALE_NOW)])
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
    uv2 = np.column_stack([np.repeat(np.array(flags), 4), np.full(4 * k, PALE_NOW)])
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
    if photo is not None and not box(X0 + 1.0, Y0 + 1.0, X1 - 1.0, Y1 - 1.0).contains(poly):
        photo = None            # past the set's edge there is no photo: plain roof
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


_MNS_S = {}
# Big tiled blocks with their roofs shaped from the LiDAR: off until the
# blocks are split into their houses (the 50 cm scan alone gives lumps).
LIDAR_ROOFS = False


def planar_roof(S, part):
    """The LiDAR roof surface inside `part` made of flat planes, the way
    real roofs are: cells sorted by which way they slope (four directions
    square to the building, or flat), each connected patch fitted with one
    plane, so slopes are true planes and ridges, hips and valleys straight
    lines where two meet. Returns (heights on the 50 cm grid, row0, col0)."""
    x0, y0, x1, y1 = part.bounds
    c0, r0 = max(int((x0 - X0) / RES) - 2, 0), max(int((Y1 - y1) / RES) - 2, 0)
    c1, r1 = min(int((x1 - X0) / RES) + 3, S.shape[1]), min(int((Y1 - y0) / RES) + 3, S.shape[0])
    z = S[r0:r1, c0:c1].astype(np.float64)
    if z.size < 20:
        return None
    xs = X0 + (c0 + np.arange(z.shape[1]) + 0.5) * RES
    ys = Y1 - (r0 + np.arange(z.shape[0]) + 0.5) * RES
    XX, YY = np.meshgrid(xs, ys)
    inside = contains_xy(part.buffer(-0.3, join_style=2), XX, YY)
    gy_, gx_ = np.gradient(ndimage.gaussian_filter(z, 2.0), RES)     # aspect from a calmer surface
    gy_ = -gy_                                   # rows run south
    c = np.asarray(part.minimum_rotated_rectangle.exterior.coords)[:3]
    e = c[1] - c[0] if np.hypot(*(c[1] - c[0])) >= np.hypot(*(c[2] - c[1])) else c[2] - c[1]
    th = math.atan2(e[1], e[0])
    ang = (np.arctan2(gy_, gx_) - th) % (2 * math.pi)
    cls = 1 + (np.round(ang / (math.pi / 2)).astype(int) % 4)
    cls[np.hypot(gx_, gy_) < 0.12] = 0
    cls[~inside] = -1
    # Majority filter: one stray cell does not split a slope.
    best, cnt = cls.copy(), np.zeros(cls.shape)
    for k in range(0, 5):
        n_ = ndimage.uniform_filter((cls == k).astype(float), 7)
        better = n_ > cnt
        best[better], cnt[better] = k, n_[better]
    best[~inside] = -1
    out = z.copy()
    for k in range(0, 5):
        lab, n_ = ndimage.label(best == k)
        for j in range(1, n_ + 1):
            mk = lab == j
            if mk.sum() < 40:
                continue
            A = np.column_stack([XX[mk] - xs[0], YY[mk] - ys[0], np.ones(mk.sum())])
            b = z[mk]
            coef = np.linalg.lstsq(A, b, rcond=None)[0]
            res = np.abs(A @ coef - b)
            keep = res < max(0.4, np.percentile(res, 80))
            if keep.sum() >= 8:
                coef = np.linalg.lstsq(A[keep], b[keep], rcond=None)[0]
            if k == 0:
                coef[:2] = 0.0
                coef[2] = float(np.median(b))
            out[mk] = np.clip((XX[mk] - xs[0]) * coef[0] + (YY[mk] - ys[0]) * coef[1] + coef[2],
                              np.percentile(b, 2), np.percentile(b, 98))
    return out, r0, c0


LIDAR_EDGE = []      # lidar_roof's rings: (x, y, roof height at the outline), for walls built to meet it


def lidar_roof(m, part, z_eave, z_cap, mns, photo, wall_col=None):
    """A roof shaped like the real one: the LiDAR surface (50 cm, lightly
    smoothed) draped inside the outline on a 1 m grid, from the eaves (the
    outline, at z_eave) up to z_cap, painted with the roof photo. For
    blocks the set builds as one footprint over many tiled roofs: their
    ridges, hips and valleys get their real volume. Returns True if built."""
    key = id(mns)
    if key not in _MNS_S:
        a = np.where(np.isfinite(mns), mns, np.nan)
        # A 2.5 m median takes out chimneys, aerials and roof kit (and the
        # scan's speckle); a light blur then keeps ridges straight.
        fill = ndimage.median_filter(np.nan_to_num(a, nan=float(np.nanmedian(a))), size=5)
        _MNS_S.clear()
        _MNS_S[key] = ndimage.gaussian_filter(fill, 0.8)
    S = _MNS_S[key]
    LIDAR_EDGE.clear()
    inner = part.buffer(-0.6, join_style=2)
    if inner.is_empty:
        return False
    x0, y0, x1, y1 = part.bounds
    gx, gy = np.meshgrid(np.arange(math.floor(x0) + 0.5, x1, 1.0), np.arange(math.floor(y0) + 0.5, y1, 1.0))
    gx, gy = gx.ravel(), gy.ravel()
    ok = contains_xy(inner, gx, gy)
    gx, gy = gx[ok], gy[ok]
    if len(gx) < 12:
        return False
    Z = planar_roof(S, part)
    if Z is None:
        return False
    zz_, r0_, c0_ = Z
    zin = ndimage.map_coordinates(zz_, [(Y1 - gy) / RES - 0.5 - r0_, (gx - X0) / RES - 0.5 - c0_], order=1, mode="nearest")
    zin = np.clip(zin, z_eave, z_cap)
    from scipy.spatial import cKDTree
    kd = cKDTree(np.column_stack([gx, gy]))
    verts, segs, vz, holes = [], [], [], []
    part = orient(part, 1.0)
    for ring in [part.exterior] + list(part.interiors):
        ln = ring.length
        pts = shapely.line_interpolate_point(ring, np.linspace(0, ln, max(4, int(ln / 1.0)), endpoint=False))
        b = len(verts)
        n_ = len(pts)
        bx_, by_ = shapely.get_x(pts), shapely.get_y(pts)
        # The roof meets the outline at the height of the roof just
        # inside it (the scan's own eaves); a strip of wall closes the gap
        # down to the walls' top.
        _, nn = kd.query(np.column_stack([bx_, by_]))
        ze = zin[nn]
        LIDAR_EDGE.append((bx_, by_, ze))
        verts.extend(zip(bx_, by_))
        vz.extend(ze.tolist())
        segs.extend([[b + i, b + (i + 1) % n_] for i in range(n_)])
        if wall_col is not None:
            j = (np.arange(n_) + 1) % n_
            for i in range(n_):
                hi_a, hi_b = ze[i], ze[j[i]]
                if max(hi_a, hi_b) - z_eave < 0.05:
                    continue
                Pq = enu_fast(np.array([bx_[i], bx_[j[i]], bx_[j[i]], bx_[i]]),
                              np.array([by_[i], by_[j[i]], by_[j[i]], by_[i]]),
                              np.array([z_eave, z_eave, hi_b, hi_a]))
                nrm = np.cross(Pq[1] - Pq[0], Pq[3] - Pq[0])
                nrm /= max(np.linalg.norm(nrm), 1e-9)
                m.add(Pq, nrm, wall_col, PLAIN, idx=np.array([0, 1, 2, 0, 2, 3]))
    for hole in part.interiors:
        hp = Polygon(hole).representative_point()
        holes.append([hp.x, hp.y])
    verts.extend(zip(gx, gy))
    vz.extend(zin.tolist())
    data = {"vertices": np.array(verts), "segments": np.array(segs)}
    if holes:
        data["holes"] = np.array(holes)
    try:
        t = triangle.triangulate(data, "p")
    except Exception:
        return False
    if "triangles" not in t or not len(t["triangles"]):
        return False
    v = t["vertices"]
    zz = np.asarray(vz, float)
    if len(v) > len(zz):
        zz = np.concatenate([zz, np.full(len(v) - len(zz), z_eave)])
    tri = t["triangles"]
    P = enu_fast(v[:, 0], v[:, 1], zz)
    # The photo leans: a ridge 4 m above the eaves shows up 4 m x lean
    # further over than the eaves do, so each vertex looks there.
    lx, ly = photo["lean"]
    dz = zz - z_eave
    uv = np.column_stack([v[:, 0] - X0 + dz * lx, Y1 - v[:, 1] - dz * ly])
    # Smooth normals (area-weighted), so the light rolls over the slopes
    # instead of picking out every LiDAR facet.
    fn_ = np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]])
    fn_ *= np.sign(fn_[:, 2:3] + 1e-9)
    N = np.zeros_like(P)
    for k_ in range(3):
        np.add.at(N, tri[:, k_], fn_)
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-9)
    photo["mesh"].add(P, N, [255, 255, 255], ROOF_FLAT, uv=uv, idx=tri, style=PHOTO_ROOF)
    photo["uvmap"].append((len(photo["mesh"].uv) - 1, len(photo["polys"])))
    photo["polys"].append((part, photo["shift"], z_eave))
    photo["tiled"].append(True)          # no roof kit on a pitched roof
    return True


def red_share(part, ortho, shift):
    """Share of the roof the IGN photo shows as terracotta tiles."""
    x0, y0, x1, y1 = part.bounds
    gx, gy = np.meshgrid(np.arange(x0, x1, 1.0), np.arange(y0, y1, 1.0))
    gx, gy = gx.ravel(), gy.ravel()
    ok = contains_xy(part, gx, gy)
    if ok.sum() < 10:
        return 0.0
    cx = np.clip(((gx[ok] + shift[0] - X0) / RES).astype(int), 0, ortho.shape[1] - 1)
    cy = np.clip(((Y1 - gy[ok] - shift[1]) / RES).astype(int), 0, ortho.shape[0] - 1)
    c = ortho[cy, cx].astype(float)
    return float(np.mean((c[:, 0] - c[:, 2] > 28) & (c[:, 0] > c[:, 1] * 1.12)))


def kmeans1d(v, k):
    c = np.percentile(v, np.linspace(10, 90, k))
    for _ in range(20):
        lab = np.argmin(np.abs(v[:, None] - c[None]), 1)
        c = np.array([v[lab == i].mean() if np.any(lab == i) else c[i] for i in range(k)])
    lab = np.argmin(np.abs(v[:, None] - c[None]), 1)
    return np.sort(c), lab


# Hand-picked looks for landmark buildings (IGN BD TOPO ids).
HEROES = {
    # Casino de Monte-Carlo and the Salle Garnier: sandy ochre stone, verdigris roofs.
    "BATIMENT0000000074070294": {"wall": "#d9c6a5", "roof": "#6fa596"},
    "BATIMENT0000000074070299": {"wall": "#d9c6a5", "roof": "#6fa596"},
}


# Casino de Monte-Carlo, as it stands (2026 street photos, IGN LiDAR 2021,
# IGN aerial photo). The front on the square: two towers with an oculus on
# each face and a square copper dome with a lantern, the clock between two
# bronze groups, dormers in the roof, Garnier's glass-and-iron marquise over
# the three arched doors and a low flight of steps. Wall colour: the sandy
# ochre of the stone in the photos (not the cream of the Hôtel de Paris).
CASINO_FRONT = "BATIMENT0000000074070299"
CASINO_SQUARE = (1056553.0, 6303373.0)      # the Casino fountain on the square, L93
CASINO_TOWER_IN = 12.5                       # m from each end of the front to a tower's axis
CASINO_TOWER_W = 7.2
# The raised roofs of the gaming halls (Salle Europe, the Salles des
# Amériques and Blanche, the private rooms) and the Salle Garnier's: seeds
# in metres east / north of the fountain where the LiDAR has each roof's
# top. Each hall's outline and height come from the LiDAR itself.
CASINO_HALLS = [(116, 59), (97, 38), (106, 30), (82, 20), (72, 2), (72, -48)]
COPPER = "#6fa596"
LIT_GLASS, ROOF_GLASS = 7, 15        # PLAIN styles (viewer: lit shop glass, glazed roof)


def _vpanel(m, o, u, n, sz, col, depth=0.3, style=None, kind=None):
    """An upright slab in the plane through o along u (n its normal): sz is
    its outline as (along, up) points, convex, anticlockwise seen from -n.
    The face looks towards -n, `depth` deep behind it."""
    kind = PLAIN if kind is None else kind
    sz = np.asarray(sz, float)
    u, n = np.asarray(u, float), np.asarray(n, float)
    o = np.asarray(o, float)
    # Wind the outline so the face's normal is -n (right-handed in ENU):
    # anticlockwise in (u, up) gives cross(u, up) = (u1, -u0).
    area = np.sum(sz[:, 0] * np.roll(sz[:, 1], -1) - np.roll(sz[:, 0], -1) * sz[:, 1])
    if (area > 0) != (u[1] * -n[0] + -u[0] * -n[1] > 0):
        sz = sz[::-1]
    col = np.asarray(col, float)
    k = len(sz)
    def ring(off):
        xy = o[None] + u[None] * sz[:, :1] + n[None] * off
        return enu_fast(xy[:, 0], xy[:, 1], sz[:, 1])
    F, B = ring(0.0), ring(depth)
    cf = F.mean(0)[None].repeat(k, 0)
    cb = B.mean(0)[None].repeat(k, 0)
    j = np.arange(k); jn = (j + 1) % k
    m.tri(cf, F[j], F[jn], col, kind, style=style)
    m.tri(cb, B[jn], B[j], col * 0.85, kind, style=style)
    # Sides, both ways round (thin pieces; cheaper than working out which).
    for a_, b_, c_ in ((F[j], F[jn], B[jn]), (F[j], B[jn], B[j])):
        m.tri(a_, b_, c_, col * 0.9, kind, style=style)
        m.tri(a_, c_, b_, col * 0.9, kind, style=style)


def dome_cap(m, c, u, n, half, z0, h, col, gold):
    """Square copper dome with rounded corners and an ogee profile, a small
    lantern on top and a gilded finial, as on the Casino's towers."""
    u, n = np.asarray(u, float), np.asarray(n, float)
    if u[0] * n[1] - u[1] * n[0] < 0:
        n = -n                                   # anticlockwise rings: normals face out
    prof = [(1.0, 0.0), (0.98, 0.12), (0.9, 0.35), (0.72, 0.6), (0.48, 0.8), (0.26, 0.93), (0.16, 1.0)]
    th = np.linspace(0, 2 * math.pi, 25)[:-1]
    ex = 0.35                                    # squareness
    sx = np.sign(np.cos(th)) * np.abs(np.cos(th)) ** ex
    sy = np.sign(np.sin(th)) * np.abs(np.sin(th)) ** ex
    rings = []
    for r, t in prof:
        xy = c[None] + (u[None] * sx[:, None] + n[None] * sy[:, None]) * half * r
        rings.append(enu_fast(xy[:, 0], xy[:, 1], np.full(len(th), z0 + h * t)))
    k = len(th)
    j = np.arange(k); jn = (j + 1) % k
    col = np.asarray(col, float)
    for i in range(len(rings) - 1):
        A, B = rings[i], rings[i + 1]
        cc = col * (0.92 + 0.05 * i)
        m.tri(A[j], A[jn], B[jn], cc, ROOF_TILE)
        m.tri(A[j], B[jn], B[j], cc, ROOF_TILE)
    # Lantern: a little drum with arched openings, then a cap and a finial.
    lw = half * 0.32
    zl = z0 + h
    lp = orient(Polygon([c + u * lw + n * lw, c - u * lw + n * lw, c - u * lw - n * lw, c + u * lw - n * lw]), 1.0)
    add_walls(m, lp, zl, zl + h * 0.32, zl, 10, np.array(hexrgb("#e8dcc2")), PLAIN)
    add_cap(m, lp.buffer(0.12, join_style=2), zl + h * 0.32, h * 0.22, col, ROOF_TILE)
    _ball(m, c[0], c[1], zl + h * 0.6, 0.16, gold, None, 8, 5)
    add_cap(m, orient(Polygon([c + u * 0.06, c + n * 0.06, c - u * 0.06, c - n * 0.06]), 1.0),
            zl + h * 0.62, h * 0.3, gold, PLAIN)


def casino_halls(m, poly, base, H, fh, col, kind, style, rcol, mns, stats):
    """The halls' roofs rising out of the copper terrace, each where and as
    high as the LiDAR sees it: a hipped roof with a copper skirt and a
    glazed top (the skylights of the gaming rooms), on an attic wall with
    windows. Also the two towers of the Salle Garnier on the sea side,
    with their domes. Returns the number of halls built."""
    zt = base + H
    x0, y0, x1, y1 = poly.bounds
    c0, c1 = max(int((x0 - X0) / RES), 0), int(math.ceil((x1 - X0) / RES))
    r0, r1 = max(int((Y1 - y1) / RES), 0), int(math.ceil((Y1 - y0) / RES))
    sub = ndimage.median_filter(np.nan_to_num(mns[r0:r1, c0:c1], nan=0.0), 3)
    copper = np.array(hexrgb(COPPER))
    glass = np.array(hexrgb("#7f9aa0"))
    done, n_ = [], 0
    for e, nn in CASINO_HALLS:
        sx, sy = CASINO_SQUARE[0] + e, CASINO_SQUARE[1] + nn
        if not poly.contains(Point(sx, sy)):
            continue
        r, c = int((Y1 - sy) / RES) - r0, int((sx - X0) / RES) - c0
        top = float(sub[max(r - 2, 0):r + 3, max(c - 2, 0):c + 3].max())
        # Lower the threshold until the hall's roof merges with the
        # terrace around it: the last outline before the jump is the hall.
        best, prev_a = None, 0.0
        for th_ in np.arange(top - 0.5, top - 12, -0.25):
            lab_, _ = ndimage.label(sub > th_)
            L_ = lab_[r, c]
            if not L_:
                continue
            cells = lab_ == L_
            a = cells.sum() * RES * RES
            if a > 1300 or (prev_a > 40 and a > 2.0 * prev_a):
                break
            best, prev_a = (cells, th_), a
        if best is None or prev_a < 25:
            continue
        cells, zb = best
        hp = cells_to_poly(cells, X0 + c0 * RES, Y1 - r0 * RES, RES)
        hp = max(to_polys(hp), key=lambda q: q.area, default=None)
        if hp is None or hp.area < 25:
            continue
        # A clean rectangle of the same area and axes (the halls are
        # rectangular; the thresholded LiDAR outline is ragged).
        rect = hp.minimum_rotated_rectangle
        k_ = math.sqrt(min(1.0, hp.area / rect.area))
        hp = shapely.affinity.scale(rect, k_, k_, origin=rect.centroid)
        hp = orient(hp.intersection(poly.buffer(-0.5)).buffer(0), 1.0) if hp.intersects(poly) else hp
        if not isinstance(hp, Polygon) or hp.area < 25 or any(hp.intersection(q).area > 0.5 * hp.area for q in done):
            continue
        done.append(hp)
        zb = max(zb, zt + 0.6)
        rise = top - zb
        if rise < 1.2:
            continue
        # Attic wall with windows from the terrace up to the eaves.
        add_walls(m, hp, zt - 0.5, zb, zt - 0.5, max(2.6, zb - zt + 0.5), col, kind, style=style)
        cor = hp.buffer(0.3, join_style=2)
        add_walls(m, cor, zb - 0.35, zb, zb, 10, np.minimum(np.asarray(col) * 1.08 + 8, 250), PLAIN)
        # Hipped roof (or pyramid) to the LiDAR's top: copper skirt, glass above.
        rr = np.asarray(hp.minimum_rotated_rectangle.exterior.coords)
        half = min(np.linalg.norm(rr[1] - rr[0]), np.linalg.norm(rr[2] - rr[1])) / 2
        tmax = max(1.0, half * 0.95)
        t_ = skeleton_roof(orient(cor, 1.0), zb, rise / tmax, tmax, dt=tmax / 4)
        if t_ is None:
            continue
        v, zz, tri = t_
        P = enu_fast(v[:, 0], v[:, 1], zz)
        zc_ = zz[tri].mean(1)
        up = zc_ > zb + rise * 0.3
        if (~up).any():
            m.tri(P[tri[~up, 0]], P[tri[~up, 1]], P[tri[~up, 2]], copper, ROOF_TILE)
        if up.any():
            m.tri(P[tri[up, 0]], P[tri[up, 1]], P[tri[up, 2]], glass, PLAIN, style=ROOF_GLASS)
        n_ += 1
    # The sea-side towers: tall LiDAR blobs well clear of the halls.
    hall_pts = [Point(CASINO_SQUARE[0] + e, CASINO_SQUARE[1] + nn) for e, nn in CASINO_HALLS]
    rel = sub - base
    ref = float(np.percentile(rel[rel > 3], 70)) if (rel > 3).any() else H
    grid = (rel > max(ref, H) + 5)
    lab_, k_ = ndimage.label(ndimage.binary_opening(grid, iterations=1))
    gold = np.array(hexrgb("#d9b75e"))
    for j in range(1, k_ + 1):
        cells = lab_ == j
        a = cells.sum() * RES * RES
        if a < 10 or a > 90:
            continue
        tp = cells_to_poly(cells, X0 + c0 * RES, Y1 - r0 * RES, RES)
        if tp.is_empty or not poly.buffer(1.0).contains(tp.centroid) or min(q.distance(tp.centroid) for q in hall_pts) < 9:
            continue
        cc = np.array([tp.centroid.x, tp.centroid.y])
        peak = float(rel[cells].max())
        # A square tower on the blob, its dome taking the top 45 %.
        w = min(7.0, max(4.5, math.sqrt(a)))
        u_ = np.array([1.0, 0.0])
        rect = np.asarray(poly.minimum_rotated_rectangle.exterior.coords)
        u_ = (rect[1] - rect[0]) / np.linalg.norm(rect[1] - rect[0])
        n_v = np.array([-u_[1], u_[0]])
        body = base + max(H + 2.0, peak - max(4.0, w * 0.9))
        tw = orient(Polygon([cc + u_ * w / 2 + n_v * w / 2, cc - u_ * w / 2 + n_v * w / 2,
                             cc - u_ * w / 2 - n_v * w / 2, cc + u_ * w / 2 - n_v * w / 2]), 1.0)
        add_walls(m, tw, zt - 1.0, body, base, fh, col, kind, style=style)
        add_walls(m, tw.buffer(0.3, join_style=2), body - 0.5, body, body, 10, np.minimum(np.asarray(col) * 1.08 + 8, 250), PLAIN)
        dome_cap(m, cc, u_, n_v, w / 2 * 0.92, body, max(2.5, base + peak - body - 1.2), copper, gold)
        stats["towers"] += 1
    return n_


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
    sk = min(3.4, H * 0.25)
    zc = zt - sk                                # main cornice; mansard above
    copper = np.array(hexrgb(COPPER))
    gold = np.array(hexrgb("#d9b75e"))
    bronze = np.array(hexrgb("#5d4a30"))
    stone = np.array(hexrgb("#e6dcc8"))
    trim = np.minimum(np.asarray(col) * 1.07 + 8, 250)
    q = poly.exterior.interpolate(poly.exterior.project(shapely.Point(*mid)))
    f0 = np.array([q.x, q.y])                    # on the façade line, mid-front
    # The square is well above the building's lowest corner (it falls away
    # to the sea behind): the entrance stands at the square's level.
    zs = base
    if mntR is not None:
        z_ = mntR.sample(np.array([f0[0] - n[0] * 8]), np.array([f0[1] - n[1] * 8]))
        if np.isfinite(z_[0]):
            zs = float(z_[0])

    def block(c, w, d):
        pts = [c + u * w / 2 + n * d / 2, c - u * w / 2 + n * d / 2, c - u * w / 2 - n * d / 2, c + u * w / 2 - n * d / 2]
        return orient(Polygon(pts), 1.0)

    def at(t, d=0.0):                            # t metres along the front from its middle
        return f0 + u * t + n * d

    # Towers: proud of the façade by 60 cm, an oculus in a stone frame on
    # each face under a cornice, then the dome.
    tw = CASINO_TOWER_W
    zte = zc + 5.6
    for s_ in (-1, 1):
        t_ = s_ * (L / 2 - CASINO_TOWER_IN)
        c = at(t_, tw / 2 - 0.6)
        tp = block(c, tw, tw)
        add_walls(m, tp, zs - 0.5, zte, zs, fh, col, kind, style=style)
        for z_, h_ in ((zc - 0.6, 0.6), (zte - 0.1, 0.55)):
            add_walls(m, tp.buffer(0.35, join_style=2), z_, z_ + h_, z_, 10, trim, PLAIN)
            for rp in to_polys(tp.buffer(0.35, join_style=2).difference(tp)):
                add_roof(m, orient(rp, 1.0), z_ + h_, trim, False)
        add_roof(m, tp, zte + 0.45, trim, False)
        # Oculi on the three faces seen from the square.
        ang = np.linspace(0, 2 * math.pi, 17)
        for fn, fu in ((-n, u), (u, n), (-u, -n)):
            fc = c + fn * (tw / 2 + 0.03)
            zo = zc + 2.6
            for rad, cl, off in ((1.25, trim, 0.06), (0.85, np.array([40.0, 44.0, 46.0]), 0.12)):
                ring = np.array([[math.cos(t) * rad, zo + math.sin(t) * rad] for t in ang[:-1]])
                _vpanel(m, fc + fn * off, fu, -fn, ring, cl, depth=0.05)
        # Balustraded parapet round the tower top, then the dome.
        dome_cap(m, c, u, n, tw / 2 * 0.86, zte + 0.45, 5.2, copper, gold)
    # The clock: a curved gable over the middle with the clock face and a
    # bronze group either side (Music and Dance, in the photos).
    gab = [(-4.6, zc - 0.2), (4.6, zc - 0.2), (4.6, zc + 2.4), (3.2, zc + 3.6), (1.4, zc + 4.3),
           (0.0, zc + 4.45), (-1.4, zc + 4.3), (-3.2, zc + 3.6), (-4.6, zc + 2.4)]
    _vpanel(m, at(0, -0.45), -u, n, [(-x, z) for x, z in gab], np.asarray(col, float), depth=1.2)
    ang = np.linspace(0, 2 * math.pi, 21)[:-1]
    for rad, cl, off in ((1.15, gold, 0.0), (0.95, np.array([244.0, 240.0, 228.0]), -0.02)):
        _vpanel(m, at(0, -0.47 + off), -u, n, [(math.cos(t) * rad, zc + 2.0 + math.sin(t) * rad) for t in ang], cl, depth=0.05)
    for s_ in (-1, 1):
        g = at(s_ * 3.4, 0.2)
        _box(m, g[0], g[1], u, 1.6, 1.0, zc, zc + 0.5, stone)
        _box(m, g[0], g[1], u, 1.0, 0.7, zc + 0.5, zc + 1.6, bronze, style=BRONZE)
        _ball(m, g[0] + u[0] * 0.25 * s_, g[1] + u[1] * 0.25 * s_, zc + 2.0, 0.42, bronze, BRONZE, 8, 5)
        _box(m, g[0] - u[0] * 0.4 * s_, g[1] - u[1] * 0.4 * s_, u, 0.4, 0.4, zc + 1.5, zc + 2.6, bronze, style=BRONZE)
    # Dormers on the mansard: round-headed, in stone, on the outer bays
    # and either side of the clock, as in the photos.
    tin = L / 2 - CASINO_TOWER_IN
    dpos = [s_ * (tin + tw / 2 + 2.4) for s_ in (-1, 1)] + [s_ * (tin + tw / 2 + 6.2) for s_ in (-1, 1)] \
        + [s_ * 6.4 for s_ in (-1, 1)]
    for t_ in dpos:
        if abs(t_) > L / 2 - 1.2:
            continue
        o = at(t_, 0.9)
        out = [(-0.95, zc - 0.1), (0.95, zc - 0.1), (0.95, zc + 2.0), (0.6, zc + 2.55), (0.0, zc + 2.8),
               (-0.6, zc + 2.55), (-0.95, zc + 2.0)]
        _vpanel(m, o, -u, n, [(-x, z) for x, z in out], np.asarray(col, float) * 1.03, depth=1.8)
        win = [(-0.5, zc + 0.35), (0.5, zc + 0.35), (0.5, zc + 1.75), (0.0, zc + 2.15), (-0.5, zc + 1.75)]
        _vpanel(m, o - n * 0.03, -u, n, [(-x, z) for x, z in win], np.array([38.0, 44.0, 48.0]), depth=0.02)
        # Its little copper roof back into the mansard.
        rp = block(o + n * 1.1, 2.1, 2.0)
        add_cap(m, rp, zc + 2.0, 0.9, copper, ROOF_TILE)
    # The entrance: three tall arched doors, glazed and lit, between
    # stone pilasters, up a broad flight of four steps.
    zf = zs + 0.5
    lit_doors(m, f0, u, n, zf, (-3.9, 0.0, 3.9), w=2.7, h=5.4)
    for j in (-1, 0, 1):
        c = at(j * 3.9, -0.06)
        for s_ in (-1, 1):
            p_ = c + u * s_ * 1.95 - n * 0.2
            _box(m, p_[0], p_[1], u, 0.7, 0.45, zs, zf + 5.4 + 0.8, trim)
    entrance_steps(m, f0, u, n, 21.5, zs)
    # Garnier's marquise over the doors.
    marquise(m, f0, u, n, 2 * (L / 2 - CASINO_TOWER_IN) - 1.0, zs, zs + 5.7, 2.2, 3.6)
    # A bronze candelabrum either side of the steps.
    for s_ in (-1, 1):
        lp = at(s_ * 11.6, -5.4)
        _box(m, lp[0], lp[1], u, 0.8, 0.8, zs, zs + 1.3, stone)
        _box(m, lp[0], lp[1], u, 0.22, 0.22, zs + 1.3, zs + 4.8, hexrgb("#3d3322"), style=BRONZE)
        for t_ in (-1, 0, 1):
            pp = lp + u * t_ * 0.6
            _ball(m, pp[0], pp[1], zs + 5.1 + (0.3 if t_ == 0 else 0), 0.26, np.array(hexrgb("#f6eedb")), 3, 8, 5)


def marquise(m, f0, u, n, W, zs, zm, d0, d1, glass="#9fb4b4", iron="#2b3533", soffit="#b9a47e", cols=True):
    """A glass canopy on an iron frame over an entrance (the Casino's, the
    Hôtel de Paris's): its front edge bowed out d0 + d1 m from the façade
    at f0 (n points into the building), W wide, at height zm, with a
    scalloped iron valance and slender columns under the ends."""
    def at(t, d=0.0):
        return f0 + u * t + n * d
    ss = np.linspace(-W / 2, W / 2, 25)
    dep = d0 + d1 * np.sqrt(np.clip(1 - (ss / (W / 2)) ** 2, 0, 1))
    front = np.array([at(s_, -d_) for s_, d_ in zip(ss, dep)])
    back = np.array([at(s_, -0.05) for s_ in ss])
    iron = np.array(hexrgb(iron))
    Fz = enu_fast(front[:, 0], front[:, 1], np.full(len(ss), zm))
    Bz = enu_fast(back[:, 0], back[:, 1], np.full(len(ss), zm + 1.6))
    Fl = enu_fast(front[:, 0], front[:, 1], np.full(len(ss), zm - 0.7))
    Fu = enu_fast(front[:, 0], front[:, 1], np.full(len(ss), zm + 0.12))
    j = np.arange(len(ss) - 1)
    gl = np.array(hexrgb(glass))
    up_ = np.cross(Bz[1] - Fz[0], Bz[0] - Fz[0])
    if up_[2] > 0:                    # this winding faces the sky
        T1, T2 = (Fz[j], Bz[j + 1], Bz[j]), (Fz[j], Fz[j + 1], Bz[j + 1])
    else:
        T1, T2 = (Fz[j], Bz[j], Bz[j + 1]), (Fz[j], Bz[j + 1], Fz[j + 1])
    m.tri(*T1, gl, PLAIN, style=ROOF_GLASS)
    m.tri(*T2, gl, PLAIN, style=ROOF_GLASS)
    sf = np.array(hexrgb(soffit))          # lit from below by the doors
    m.tri(T1[0], T1[2], T1[1], sf, PLAIN)          # underside
    m.tri(T2[0], T2[2], T2[1], sf, PLAIN)
    m.tri(Fl[j], Fl[j + 1], Fu[j + 1], iron, PLAIN)            # edge band, both faces
    m.tri(Fl[j], Fu[j + 1], Fu[j], iron, PLAIN)
    m.tri(Fl[j], Fu[j + 1], Fl[j + 1], iron, PLAIN)
    m.tri(Fl[j], Fu[j], Fu[j + 1], iron, PLAIN)
    # Valance: a small pointed lambrequin under each panel.
    mids = (front[j] + front[j + 1]) / 2
    Tm = enu_fast(mids[:, 0], mids[:, 1], np.full(len(j), zm - 1.15))
    m.tri(Fl[j], Fl[j + 1], Tm, iron, PLAIN)
    m.tri(Fl[j], Tm, Fl[j + 1], iron, PLAIN)
    if cols:
        for i in (1, len(ss) - 2):
            cp = front[i] + (back[i] - front[i]) * 0.12
            _box(m, cp[0], cp[1], u, 0.18, 0.18, zs + 0.5, zm, iron)
            _box(m, cp[0], cp[1], u, 0.4, 0.4, zs + 0.5, zs + 0.9, iron)
    # Iron ribs from the façade to the edge.
    for i in range(0, len(ss), 3):
        _box(m, *((front[i] + back[i]) / 2), front[i] - back[i], float(np.linalg.norm(front[i] - back[i])),
             0.12, zm + 0.0, zm + 0.25, iron)


def entrance_steps(m, f0, u, n, W, zs, k=4, rise=0.13, tread=0.42, col="#e6dcc8"):
    stone = np.array(hexrgb(col))
    for i in range(k):
        d0 = tread * (k - i)
        c = f0 - n * (d0 / 2 + 0.02)
        pts = [c + u * (W - i * 0.7) / 2 + n * d0 / 2, c - u * (W - i * 0.7) / 2 + n * d0 / 2,
               c - u * (W - i * 0.7) / 2 - n * d0 / 2, c + u * (W - i * 0.7) / 2 - n * d0 / 2]
        bk = orient(Polygon(pts), 1.0)
        add_walls(m, bk, zs - 0.4, zs + rise * (i + 1), zs, 10, stone * (0.93 + 0.025 * i), PLAIN)
        add_roof(m, bk, zs + rise * (i + 1), stone * (0.98 + 0.01 * i), False)


def lit_doors(m, f0, u, n, zf, xs, w=2.6, h=4.8):
    """Tall arched glazed doors, lit from inside, on the façade at f0."""
    for x in xs:
        c = f0 + u * x - n * 0.06
        out = [(-w / 2, zf), (w / 2, zf), (w / 2, zf + h - w / 2)] + \
              [(math.cos(t) * w / 2, zf + h - w / 2 + math.sin(t) * w / 2) for t in np.linspace(0, math.pi, 9)[1:-1]] + \
              [(-w / 2, zf + h - w / 2)]
        _vpanel(m, c, -u, n, [(-x_, z) for x_, z in out], np.array(hexrgb("#6e5434")), depth=0.05, style=LIT_GLASS)
        _vpanel(m, c - n * 0.02, -u, n, [(-0.06, zf), (0.06, zf), (0.06, zf + h - 0.2), (-0.06, zf + h - 0.2)],
                np.array(hexrgb("#2a2620")), depth=0.02)


# Hôtel de Paris (1864, Belle Époque front on the square): white stone,
# the entrance under a black iron-and-glass marquise up a short flight of
# steps, square domes on the attic at the rounded corner and over the
# middle, the low glazed veranda of the bar at the north end (BD TOPO
# 09013188). Positions from the 2026 street photos.
HDP_MAIN = "BATIMENT0000002009013201"
HDP_VERANDA = "BATIMENT0000002009013188"
HDP_FRONT = ((8, -48), (-46, -11))           # the square front, metres from the fountain
HDP_DOOR_T = 14.0                            # m from the south end of that front
HDP_DOMES = (2.5, 27.0)                      # m along the front: corner and middle domes


def _rel(e, n):
    return np.array([CASINO_SQUARE[0] + e, CASINO_SQUARE[1] + n], float)


def hotel_de_paris(m, poly, base, H, mns, mntR, stats):
    a, b = _rel(*HDP_FRONT[0]), _rel(*HDP_FRONT[1])
    L = float(np.linalg.norm(b - a))
    u = (b - a) / L
    n = np.array([-u[1], u[0]])
    if np.dot(n, np.asarray(CASINO_SQUARE) - (a + b) / 2) > 0:
        n = -n                                   # into the building
    q = poly.exterior.interpolate(poly.exterior.project(Point(*(a + u * HDP_DOOR_T))))
    f0 = np.array([q.x, q.y])
    zs = float(mntR.sample(np.array([f0[0] - n[0] * 6]), np.array([f0[1] - n[1] * 6]))[0])
    if not np.isfinite(zs):
        return
    entrance_steps(m, f0, u, n, 9.0, zs, k=3, rise=0.15, tread=0.45)
    lit_doors(m, f0, u, n, zs + 0.45, (-2.4, 0.0, 2.4), w=1.8, h=3.7)
    marquise(m, f0, u, n, 11.0, zs, zs + 5.0, 1.8, 2.6, glass="#7d8c8c", iron="#1d2221", soffit="#c9b28a")
    # Domes on the attic: square, slate-grey with gilt finials.
    slate = np.array(hexrgb("#5b6466"))
    gold = np.array(hexrgb("#d9b75e"))
    stone = np.array(hexrgb("#f2eee6"))
    for t_ in HDP_DOMES:
        q = poly.exterior.interpolate(poly.exterior.project(Point(*(a + u * t_))))
        c = np.array([q.x, q.y]) + n * 3.2
        zr = mns[int((Y1 - c[1] - n[1] * 2) / RES), int((c[0] + n[0] * 2 - X0) / RES)]
        ztop = base + H if not np.isfinite(zr) else max(float(zr), base + H * 0.85)
        w = 6.0
        tp = orient(Polygon([c + u * w / 2 + n * w / 2, c - u * w / 2 + n * w / 2,
                             c - u * w / 2 - n * w / 2, c + u * w / 2 - n * w / 2]), 1.0)
        add_walls(m, tp, ztop - 3.0, ztop + 2.2, base, 3.6, stone, WALL_OLD, style=S_BELLE)
        add_walls(m, tp.buffer(0.3, join_style=2), ztop + 2.0, ztop + 2.5, ztop + 2.0, 10, stone, PLAIN)
        add_roof(m, tp.buffer(0.3, join_style=2), ztop + 2.5, stone, False)
        dome_cap(m, c, u, n, w / 2 * 0.86, ztop + 2.5, 4.4, slate, gold)
    stats["hdp"] = 1


def glass_veranda(m, poly, mntR, h, stats, iron="#2a302e"):
    """A low glass-and-iron veranda on a stone plinth (the Café de Paris's
    and the Hôtel de Paris bar's): lit glass between iron posts every
    1.6 m, a cornice, a glazed roof."""
    ring = np.asarray(orient(poly, 1.0).exterior.coords)
    s_ = np.linspace(0, poly.exterior.length, max(8, int(poly.exterior.length / 2)))
    pp = shapely.line_interpolate_point(poly.exterior, s_)
    z_ = mntR.sample(shapely.get_x(pp), shapely.get_y(pp))
    if not np.isfinite(z_).any():
        return 0
    z0 = float(np.nanmax(z_))
    zlo = float(np.nanmin(z_)) - 0.3
    stone = np.array(hexrgb("#e9e1d0"))
    ir = np.array(hexrgb(iron))
    add_walls(m, poly, zlo, z0 + 0.5, z0, 10, stone, PLAIN)
    for a, b in zip(ring[:-1], ring[1:]):
        L = float(np.linalg.norm(b - a))
        if L < 0.8:
            continue
        u = (b - a) / L
        out = np.array([u[1], -u[0]])
        o = (a + b) / 2 + out * 0.02
        # The glass comes down to this side's own ground (the Allée side
        # slopes away: no blank plinth wall under the windows).
        ze = mntR.sample(np.array([a[0], b[0], (a[0] + b[0]) / 2]) + out[0] * 0.8,
                         np.array([a[1], b[1], (a[1] + b[1]) / 2]) + out[1] * 0.8)
        zl = min(z0, float(np.nanmin(ze))) if np.isfinite(ze).any() else z0
        zg = zl + 0.5
        _vpanel(m, o, u, -out, [(-L / 2, zg), (L / 2, zg), (L / 2, z0 + h - 0.6), (-L / 2, z0 + h - 0.6)],
                np.array(hexrgb("#e3bd82")), depth=0.02, style=LIT_GLASS)
        k = max(1, int(round(L / 1.6)))
        for i in range(k + 1):
            p = a + u * (L * i / k) + out * 0.06
            _box(m, p[0], p[1], u, 0.14, 0.14, zg, z0 + h - 0.6, ir)
        if zl < z0 - 0.6:
            # A transom bar where the floor inside sits above the street.
            _vpanel(m, o + out * 0.05, u, -out, [(-L / 2, z0 + 0.4), (L / 2, z0 + 0.4), (L / 2, z0 + 0.55), (-L / 2, z0 + 0.55)],
                    ir, depth=0.03)
        _vpanel(m, o + out * 0.05, u, -out, [(-L / 2, z0 + 2.35), (L / 2, z0 + 2.35), (L / 2, z0 + 2.45), (-L / 2, z0 + 2.45)],
                ir, depth=0.03)
    cor = orient(poly.buffer(0.35, join_style=2), 1.0)
    add_walls(m, cor, z0 + h - 0.6, z0 + h, z0 + h - 0.6, 10, ir, PLAIN)
    t_ = skeleton_roof(cor, z0 + h, 0.25, 3.0, dt=1.0)
    if t_ is not None:
        v, zz, tri = t_
        P = enu_fast(v[:, 0], v[:, 1], zz)
        m.tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], np.array(hexrgb("#8fa3a3")), PLAIN, style=ROOF_GLASS)
    else:
        add_roof(m, cor, z0 + h, np.array(hexrgb("#8fa3a3")), False)
    stats["verandas"] = stats.get("verandas", 0) + 1
    return 1


# Café de Paris: its glass-and-iron veranda on the square (BD TOPO
# 85035404, with the little corner pavilion 74070381), and the terrace in
# front of it under white parasols, between the palms.
CDP_VERANDA = ("BATIMENT0000000085035404", "BATIMENT0000000074070381")
CDP_MAIN = "BATIMENT0000000074070358"
CDP_TERRACE_EDGE = ((3, 35), (27, 19))       # the veranda's side on the square
CDP_TERRACE_DEPTH = 9.0


def cafe_terrace(m, mntR, ways, pts, stats):
    """The Café de Paris terrace as in the 2026 street photos: two rows of
    big square white parasols, edge to edge, hugging the veranda (one low
    white canopy seen from the square), tables and chairs under them, and
    a low black railing with planters along the square side."""
    a, b = _rel(*CDP_TERRACE_EDGE[0]), _rel(*CDP_TERRACE_EDGE[1])
    L = float(np.linalg.norm(b - a))
    u = (b - a) / L
    out = np.array([u[1], -u[0]])
    if np.dot(out, np.asarray(CASINO_SQUARE) - (a + b) / 2) < 0:
        out = -out
    palms = np.array([xy for xy, t in pts if t.get("natural") == "tree"
                      and casino_palm_zone().contains(Point(*xy))] or [[1e9, 1e9]])
    white = np.array(hexrgb("#f4f1ea"))
    pole = np.array(hexrgb("#b9b2a4"))
    cloth = np.array(hexrgb("#ece6da"))
    chair = np.array(hexrgb("#7a5a3a"))
    iron = np.array(hexrgb("#1d2220"))
    green = np.array(hexrgb("#4f6b3a"))
    P_, H_ = 3.4, 1.62                    # parasol pitch and half-width
    rows = (2.0, 2.0 + P_)
    depth = rows[-1] + H_ + 0.4
    z_edge = mntR.sample(np.array([a[0], b[0]]), np.array([a[1], b[1]]))
    n_ = 0
    for i in np.arange(H_ + 0.3, L - H_, P_):
        for d in rows:
            c = a + u * i + out * d
            if np.min(np.hypot(*(palms - c).T)) < 1.0 or not off_track(c[0], c[1], 2.5):
                continue
            z = float(mntR.sample(np.array([c[0]]), np.array([c[1]]))[0])
            if not np.isfinite(z):
                continue
            # The parasol: a shallow white pyramid on a pole, 2.7 m up.
            _box(m, c[0], c[1], u, 0.08, 0.08, z, z + 2.75, pole)
            sq = orient(Polygon([c + u * H_ + out * H_, c - u * H_ + out * H_, c - u * H_ - out * H_,
                                 c + u * H_ - out * H_]), 1.0)
            add_cap(m, sq, z + 2.45, 0.45, white, PLAIN)
            add_roof(m, orient(sq, -1.0), z + 2.45, white * 0.78, False)
            # Two tables with their chairs under it.
            for k in (-0.8, 0.8):
                t = c + u * k
                _box(m, t[0], t[1], u, 0.7, 0.7, z + 0.72, z + 0.76, cloth)
                _box(m, t[0], t[1], u, 0.07, 0.07, z, z + 0.72, iron)
                for sd in (-0.55, 0.55):
                    q = t + out * sd
                    _box(m, q[0], q[1], u, 0.42, 0.4, z + 0.42, z + 0.47, chair)
                    r = t + out * (sd * 1.55)
                    _box(m, r[0], r[1], u, 0.42, 0.06, z, z + 0.9, chair)
            n_ += 1
    # The railing and planters along the square side of the terrace.
    for i in np.arange(0.0, L, 2.0):
        p0 = a + u * i + out * depth
        p1 = a + u * min(L, i + 2.0) + out * depth
        mid = (p0 + p1) / 2
        if not off_track(mid[0], mid[1], 2.0) or np.min(np.hypot(*(palms - mid).T)) < 0.8:
            continue
        z = float(mntR.sample(np.array([mid[0]]), np.array([mid[1]]))[0])
        if not np.isfinite(z):
            continue
        _box(m, mid[0], mid[1], u, 2.0, 0.05, z + 0.95, z + 1.0, iron)
        _box(m, p0[0], p0[1], u, 0.06, 0.06, z, z + 1.0, iron)
        if int(i / 2.0) % 2 == 0:
            q = mid - out * 0.45
            _box(m, q[0], q[1], u, 0.9, 0.5, z, z + 0.55, np.array(hexrgb("#d8d2c4")))
            _box(m, q[0], q[1], u, 0.8, 0.4, z + 0.55, z + 1.0, green)
    stats["cafe_parasols"] = n_
    return n_


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
    # (Not the OSM hotel point: it lies in the hotel's garden, outside every
    # outline. See metropole().)
    ("Hôtel Métropole", (1056600, 6303575), S_BELLE, "#eee2c8"),
    ("Hôtel Métropole", (1056599, 6303588), S_BELLE, "#eee2c8"),
    ("Hôtel Métropole", (1056558, 6303555), S_BELLE, "#eee2c8"),
    ("One Monte-Carlo", (1056448, 6303367), S_MODERN, "#eeede8"),
    ("One Monte-Carlo", (1056433, 6303407), S_MODERN, "#eeede8"),
    ("Fairmont", (1056743, 6303450), S_MODERN, "#ece5d6"),
    ("Théâtre Princesse Grace", (1056443, 6303173), S_DECO, "#efe2c8"),
    ("Yacht Club de Monaco", (1056540, 6303156), S_RIBBON, "#f4f3ef"),   # white decks like a yacht
    ("Église Saint-Charles", (1056513, 6303731), S_ARCH, "#e9dcc4"),
    # Place d'Armes (street photos 2019-2025): the salmon-pink block with
    # cream trim on its white arcade, and the cream market hall.
    ("Place d'Armes north side", (1055948, 6302612), S_BELLE, "#f2b89f"),
    ("Halles du marché de la Condamine", (1055992, 6302585), S_ARCH, "#eed8b6"),
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
# Rond-Point du Portier (Avenue Princesse Grace / Boulevard Louis II), under
# the Boulevard du Larvotto viaduct. Rebuilt with Mareterra after the 2021
# LiDAR and the IGN photo, which both show a building site here: humps of
# earth and smudges of dirt that the film set turned into lumpy lawns, and
# the circuit's red brick pavements spread over the island. As it stands
# (Mapillary 2025, BD TOPO 2024, OpenStreetMap): pale stone paving round
# the roundabout, and on the island, east of the viaduct, Neuehouse, a
# low building of two glazed storeys between thick travertine slabs that
# overhang them, on slender pale columns.
PORTIER = None
PORTIER_NAME = "Rond-Point du Portier"
PORTIER_PAVING = (217, 210, 196)
NEUEHOUSE = "BATIMENT0000002476006808"
TRAVERTINE = "#e8dfcc"


def portier_setup(ways, mnt, mns, ortho, sea):
    """The roundabout and a margin round it: smooth ground, no surface
    humps (so no stray trees or baked shadows), paving in the photo."""
    from shapely.ops import polygonize
    ring = [LineString(p_) for t, p_, c in ways
            if t.get("name") == PORTIER_NAME and t.get("junction") == "roundabout" and len(p_) > 1]
    # (All its pieces, the one that dips under Neuehouse included, so the
    # ring closes.)
    if not ring:
        return None
    polys = list(polygonize(unary_union(ring)))
    if not polys:
        return None
    inner = max(polys, key=lambda q: q.area)
    zone = inner.buffer(14.0)
    # Not the buildings round it, nor the viaduct (its deck height comes
    # from the surface model) and its piers.
    from shapely.geometry import shape as _shape
    bridges = [LineString(p_).buffer(7.5) for t, p_, c in ways
               if t.get("bridge") and t.get("layer", "0") != "0" and len(p_) > 1]
    keep_out = list(bridges)
    for f in json.load(open(RAW / "bdtopo.json"))["features"]:
        if f["properties"].get("cleabs") == NEUEHOUSE:
            continue
        g = shapely.force_2d(_shape(f["geometry"])).buffer(0)
        if g.intersects(zone):
            keep_out.append(g.buffer(1.5))
    zone = zone.difference(unary_union(keep_out))
    zone = unary_union([q for q in to_polys(zone) if q.area > 40])
    zm = raster_mask(zone) & ~sea & np.isfinite(mnt)
    z = ndimage.median_filter(np.nan_to_num(mnt, nan=0.0), 9)
    z = ndimage.gaussian_filter(z, 2.0)
    mnt[zm] = z[zm]
    mns[zm] = z[zm]
    ortho[zm] = PORTIER_PAVING
    # Under the viaduct: level and pave the ground too, but leave the
    # surface model alone so the deck keeps its height.
    under = unary_union(bridges).intersection(inner.buffer(24.0)) if bridges else None
    if under is not None and not under.is_empty:
        um = raster_mask(under) & ~sea & np.isfinite(mnt) & ~zm
        mnt[um] = z[um]
        ortho[um] = PORTIER_PAVING
        print(f"Rond-Point du Portier: {under.area:.0f} m2 paved under the viaduct")
    print(f"Rond-Point du Portier: {zone.area:.0f} m2 levelled and paved")
    return {"zone": zone, "island": inner.buffer(-ROAD_W["secondary"] / 2)}


def neuehouse(m, poly, mntR):
    """Neuehouse on the Portier roundabout's island: two glazed storeys
    set back under thick travertine slabs that overhang them, pale square
    columns at the slab edges, a roof terrace with planters."""
    ring = poly.exterior
    s = np.linspace(0, ring.length, max(8, int(ring.length)))
    q = shapely.line_interpolate_point(ring, s)
    gz = mntR.sample(shapely.get_x(q), shapely.get_y(q))
    base = float(np.nanmin(gz)) if np.isfinite(gz).any() else 8.0
    trav = np.array(hexrgb(TRAVERTINE))
    glass = np.array(hexrgb("#55666c"))
    leaf = np.array(hexrgb("#4a6a34"))
    tiers = [(-1.6, 0.0, 4.1, 4.85, 0.55), (-1.1, 4.85, 8.2, 9.1, 0.8)]
    for inset, z0, z1, z2, over in tiers:
        g = poly.buffer(inset, join_style=2)
        for gp in to_polys(g):
            gp = orient(gp, 1.0)
            add_walls(m, gp, base + z0 - 0.4, base + z1, base, 4.1, glass, WALL_GLASS, style=S_GLASS)
        sl = poly.buffer(over, join_style=2)
        for sp in to_polys(sl):
            sp = orient(sp, 1.0)
            add_walls(m, sp, base + z1, base + z2, base + z1, 100, trav, PLAIN)
            add_roof(m, sp, base + z2, trav * (0.98 if z2 > 6 else 1.0), False)
            # Columns at the slab edge, every 6.5 m.
            cr = sp.buffer(-0.45, join_style=2).exterior
            for d in np.arange(2.0, cr.length, 6.5):
                c = cr.interpolate(d)
                c2 = cr.interpolate(min(d + 0.5, cr.length))
                ax = np.array([c2.x - c.x, c2.y - c.y])
                _box(m, c.x, c.y, ax, 0.45, 0.45, base + z0 - 0.2, base + z1, trav * 0.97)
    # Roof terrace: planters along the edge.
    pr = poly.buffer(-1.6, join_style=2)
    if isinstance(pr, Polygon) and pr.exterior.length > 10:
        ex = pr.exterior
        for d in np.arange(3.0, ex.length, 7.0):
            c = ex.interpolate(d)
            c2 = ex.interpolate(min(d + 0.5, ex.length))
            ax = np.array([c2.x - c.x, c2.y - c.y])
            _box(m, c.x, c.y, ax, 2.4, 0.9, base + 9.1, base + 9.6, trav * 0.95)
            _box(m, c.x, c.y, ax, 2.2, 0.75, base + 9.6, base + 10.2, leaf, kind=LEAF)
    return poly


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


# ------------------------------------------------------------ Grimaldi Forum
# The congress centre on the Larvotto seafront (Ferrier & Notari, 2000):
# most of it lies under the ground; above it shows a long hull clad in
# green metal, a ribbon of glass under a jutting cornice, and at its south
# end the faceted glass hall of the entrance, which slopes down to the
# esplanade. Volumes from the LiDAR, outline from BD TOPO, the glass hall's
# outline from the 20 cm photo, colours from street photos (Mapillary).
GF_PARTS = ("BATIMENT0000000074070278", "BATIMENT0000000324292897",
            # two small blocks BD TOPO draws inside its outline (a kiosk on
            # the east terrace, a stair head on the south steps)
            "BATIMENT0000002476006697", "BATIMENT0000002476006736")
GF_GLASS = [(1056820.5, 6303839.1), (1056828.1, 6303842.5), (1056833.1, 6303856.9), (1056836.8, 6303858.5),
            (1056852.0, 6303858.0), (1056852.0, 6303814.0), (1056847.2, 6303814.3), (1056841.3, 6303816.2),
            (1056834.7, 6303818.7), (1056829.2, 6303820.8)]
GF_GREEN, GF_TRIM = "#3f6c49", "#4c7b55"
GF_HALL, GF_RIBBON = "#9cc9e0", "#2c3b42"
GF_TIERS = (12.8, 18.0)            # NGF m: the LiDAR's roof levels split here
GF_HALL_EAVE, GF_HALL_TOP, GF_HALL_RUN = 8.2, 16.2, 6.0
FORUM_GLASS, FORUM_CLAD = 13, 14   # PLAIN styles (viewer: faceted glass, green cladding)


def _ring_offset(pts, o):
    """Closed ring (n, 2), CCW, each vertex pushed out by o along the mitre."""
    a = np.roll(pts, 1, 0)
    b = np.roll(pts, -1, 0)
    def nrm(d):
        d = d / np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
        return np.column_stack([d[:, 1], -d[:, 0]])
    n1, n2 = nrm(pts - a), nrm(b - pts)
    bis = n1 + n2
    bis /= np.maximum(np.linalg.norm(bis, axis=1, keepdims=True), 1e-9)
    cosh = np.clip(np.sum(bis * n1, 1), 0.5, 1.0)
    return pts + bis * (o / cosh)[:, None]


def _loft(m, pts, zo, col, style, uv2=None):
    """Wall rings: zo = [(z, offset), ...] bottom to top; uv = (m along, z)."""
    pts = np.asarray(pts, float)
    n = len(pts)
    along = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(np.vstack([pts, pts[:1]]), axis=0), axis=1))])
    j0 = np.arange(n)
    j1 = (j0 + 1) % n
    for (za, oa), (zb, ob) in zip(zo[:-1], zo[1:]):
        A = _ring_offset(pts, oa)
        B_ = _ring_offset(pts, ob)
        a0 = enu_fast(A[j0, 0], A[j0, 1], np.full(n, za)); a1 = enu_fast(A[j1, 0], A[j1, 1], np.full(n, za))
        b0 = enu_fast(B_[j0, 0], B_[j0, 1], np.full(n, zb)); b1 = enu_fast(B_[j1, 0], B_[j1, 1], np.full(n, zb))
        u0, u1 = along[j0], along[j0] + np.linalg.norm(pts[j1] - pts[j0], axis=1)
        uva = np.stack([np.column_stack([u0, np.full(n, za)]), np.column_stack([u1, np.full(n, za)]),
                        np.column_stack([u1, np.full(n, zb)])], 1)
        uvb = np.stack([np.column_stack([u0, np.full(n, za)]), np.column_stack([u1, np.full(n, zb)]),
                        np.column_stack([u0, np.full(n, zb)])], 1)
        k0 = len(m.uv2)
        m.tri(a0, a1, b1, col, PLAIN, uv=uva, style=style)
        m.tri(a0, b1, b0, col, PLAIN, uv=uvb, style=style)
        if uv2 is not None:
            for k in range(k0, len(m.uv2)):
                m.uv2[k] = np.tile(np.asarray(uv2, np.float32), (len(m.uv2[k]), 1))


def _flat_ring(m, outer, inner, z, col):
    """A flat band at z between two rings with the same vertices (cornice tops)."""
    n = len(outer)
    j0 = np.arange(n)
    j1 = (j0 + 1) % n
    o0 = enu_fast(outer[j0, 0], outer[j0, 1], np.full(n, z)); o1 = enu_fast(outer[j1, 0], outer[j1, 1], np.full(n, z))
    i0 = enu_fast(inner[j0, 0], inner[j0, 1], np.full(n, z)); i1 = enu_fast(inner[j1, 0], inner[j1, 1], np.full(n, z))
    m.tri(o0, o1, i1, col, PLAIN)
    m.tri(o0, i1, i0, col, PLAIN)


def _soffit(m, inner, outer, z, col):
    n = len(outer)
    j0 = np.arange(n)
    j1 = (j0 + 1) % n
    o0 = enu_fast(outer[j0, 0], outer[j0, 1], np.full(n, z)); o1 = enu_fast(outer[j1, 0], outer[j1, 1], np.full(n, z))
    i0 = enu_fast(inner[j0, 0], inner[j0, 1], np.full(n, z)); i1 = enu_fast(inner[j1, 0], inner[j1, 1], np.full(n, z))
    m.tri(o0, i1, o1, col, PLAIN)
    m.tri(o0, i0, i1, col, PLAIN)


def forum_hull(m, part, zb, zt, top):
    """One tier's walls: green cladding (on the top tier a hull that leans
    out towards the roof), a ribbon of dark glass, a jutting cornice."""
    green, trim = np.array(hexrgb(GF_GREEN)), np.array(hexrgb(GF_TRIM))
    for ring in [part.exterior]:
        pts = np.asarray(orient(Polygon(ring), 1.0).exterior.coords)[:-1, :2]
        h = zt - zb
        if h < 2.0:
            continue
        lean = 1.3 if top else 0.0
        rib = min(3.0 if top else 2.2, max(h - 2.5, 0.0))
        zr = zt - 1.2 - rib
        zo = [(zb, 0.0)]
        if top:
            zs = zt - 13.0
            for t_ in (0.0, 0.3, 0.55, 0.78, 1.0):
                z_ = zs + (zr - zs) * t_
                if z_ > zb + 0.3:
                    zo.append((z_, lean * t_ * t_))
        if zo[-1][0] < zr - 0.05:
            zo.append((zr, lean))
        _loft(m, pts, zo, green, FORUM_CLAD)
        if rib > 0.5:
            _loft(m, pts, [(zr, lean), (zt - 1.2, lean)], hexrgb(GF_RIBBON), FORUM_GLASS, uv2=(1.0, 0.0))
        co = lean + (0.55 if top else 0.35)
        _soffit(m, _ring_offset(pts, lean), _ring_offset(pts, co), zt - 1.2, trim * 0.7)
        _loft(m, pts, [(zt - 1.2, co), (zt + 0.6, co)], trim, None)
        _flat_ring(m, _ring_offset(pts, co), _ring_offset(pts, -0.3), zt + 0.6, trim * 1.04)
        _loft(m, _ring_offset(pts, -0.3)[::-1], [(zt, 0.0), (zt + 0.6, 0.0)], trim * 0.85, None)


def forum_hall(m, mntR):
    """The entrance: a hall of glass, flat on top and faceted down to the
    street and the esplanade like a cut crystal (LiDAR: 16 m NGF on top,
    eaves at the esplanade's level)."""
    g = orient(Polygon(GF_GLASS), 1.0)
    ring = np.asarray(g.exterior.coords)[:-1, :2]
    gz = mntR.sample(ring[:, 0], ring[:, 1])
    base = float(np.nanmin(gz)) if np.isfinite(gz).any() else 6.0
    col = np.array(hexrgb(GF_HALL))
    _loft(m, ring, [(base - 1.0, 0.0), (GF_HALL_EAVE, 0.0)], col, FORUM_GLASS)
    slope = (GF_HALL_TOP - GF_HALL_EAVE) / GF_HALL_RUN
    t = skeleton_roof(g, GF_HALL_EAVE, slope, GF_HALL_RUN, dt=GF_HALL_RUN / 2)
    if t is not None:
        v, zz, tri = t
        P = enu_fast(v[:, 0], v[:, 1], zz)
        # uv: plan metres for the facets' mullion lattice.
        uv = np.column_stack([v[:, 0] - X0, v[:, 1] - Y0])
        m.tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], col, PLAIN,
              uv=np.stack([uv[tri[:, 0]], uv[tri[:, 1]], uv[tri[:, 2]]], 1), style=FORUM_GLASS)
    # A slim steel frame along the eaves.
    _loft(m, ring, [(GF_HALL_EAVE - 0.25, 0.08), (GF_HALL_EAVE + 0.05, 0.08)], hexrgb("#d9dedf"), None)
    return g


def grimaldi_forum(m, polys, mns, mntR, photo, stats):
    """Builds the Forum from its BD TOPO parts; returns its outlines."""
    hall = forum_hall(m, mntR)
    body = unary_union([p.buffer(0.3, join_style=2) for p in polys]).buffer(-0.3, join_style=2)
    body = body.difference(hall.buffer(0.05, join_style=2))
    body = unary_union([tidy_outline(orient(q, 1.0), 0.8) for q in to_polys(body) if q.area > 30])
    ring = body.boundary
    s = np.linspace(0, ring.length, int(ring.length))
    p_ = shapely.line_interpolate_point(ring, s)
    gz = mntR.sample(shapely.get_x(p_), shapely.get_y(p_))
    base = float(np.nanmin(gz))
    # Tiers from the LiDAR roof heights (NGF): the long hull at about 21 m,
    # the north wing and the south steps at about 15, terraces lower.
    x0, y0, x1, y1 = body.bounds
    c0, c1 = int((x0 - X0) / RES), int(math.ceil((x1 - X0) / RES))
    r0, r1 = int((Y1 - y1) / RES), int(math.ceil((Y1 - y0) / RES))
    sub = mns[r0:r1, c0:c1]
    gx, gy = cell_xy(r0 + np.arange(sub.shape[0])[:, None], c0 + np.arange(sub.shape[1])[None, :])
    gx, gy = np.broadcast_arrays(gx, gy)
    ins = contains_xy(body, gx, gy) & np.isfinite(sub)
    tiers = []
    prev = body
    for li, thr in enumerate((-99.0,) + GF_TIERS):
        mk = ins & (sub >= thr)
        if li:
            mk = ndimage.binary_opening(ndimage.binary_closing(mk, iterations=3), iterations=3) & ins
            reg = cells_to_poly(mk, X0 + c0 * RES, Y1 - r0 * RES, RES).intersection(prev)
            reg = unary_union([q for q in to_polys(reg) if q.area > 80])
            if reg.is_empty:
                break
        else:
            reg = body
        nxt = (ins & (sub >= GF_TIERS[li])) if li < len(GF_TIERS) else np.zeros_like(ins)
        own = mk & ~nxt
        zt = float(np.percentile(sub[own], 75)) if own.sum() > 20 else float(np.percentile(sub[mk], 75))
        if tiers and zt < tiers[-1][1] + 2.0:
            break
        tiers.append((reg, zt))
        prev = reg
    zb = base - 1.0
    lx, ly = photo["lean"] if photo is not None else (0.0, 0.0)
    for li, (reg, zt) in enumerate(tiers):
        top = li == len(tiers) - 1
        for part in to_polys(reg):
            part = orient(part.simplify(0.4), 1.0)
            forum_hull(m, part, zb, zt, top)
            ph = None if photo is None else dict(photo, shift=((zt - base) * lx, (zt - base) * ly))
            add_roof(m, part, zt, np.array(hexrgb(GF_GREEN)), False, photo=ph)
        zb = zt
    stats["landmarks"] = stats.get("landmarks", 0) + 1
    stats["forum_tiers"] = [round(z, 1) for _, z in tiers]
    print("Grimaldi Forum tiers (NGF):", stats["forum_tiers"])
    return [orient(q, 1.0) for q in to_polys(body)] + [hall]


def rocher_cliff_top(poly, mntR):
    """A building on the edge of Le Rocher's cliffs: BD TOPO's outline
    often reaches over the edge, and walls from the lowest ground on it
    hung a tall façade down the rock. Keep the part on the cliff top (and
    a 1.5 m walk along the edge, the old town's path round the Rock)."""
    ring = poly.exterior
    s_ = np.linspace(0, ring.length, max(8, int(ring.length)))
    p_ = shapely.line_interpolate_point(ring, s_)
    z = mntR.sample(shapely.get_x(p_), shapely.get_y(p_))
    if np.isnan(z).all():
        return poly
    if np.nanpercentile(z, 50) - np.nanmin(z) < 15.0:
        return poly
    x0, y0, x1, y1 = poly.bounds
    xs = np.arange(x0 - 1, x1 + 1, RES)
    ys = np.arange(y1 + 1, y0 - 1, -RES)
    gx, gy = np.meshgrid(xs, ys)
    zz = mntR.sample(gx.ravel(), gy.ravel()).reshape(gx.shape)
    ztop = float(np.nanpercentile(z, 60))
    keep = np.nan_to_num(zz, nan=-99.0) >= ztop - 3.0
    top = cells_to_poly(keep, xs[0] - RES / 2, ys[0] + RES / 2, RES)
    q = poly.intersection(top.buffer(-1.5))
    parts = [g for g in to_polys(q) if g.area >= 10]
    if not parts:
        return None
    return orient(max(parts, key=lambda g: g.area), 1.0)


def buildings(m, feats, mns, mnt, ortho, stats, facades, photo=None):
    global BAY_NOW, GROUND_R, SHOP_NOW, ARCADE_NOW, PALE_NOW
    mntR = Raster(mnt)
    GROUND_R = mntR
    # Shops whose point lies inside some outline belong to that building only.
    in_any = set()
    if SHOPS is not None and len(SHOPS["pts"]):
        allb = unary_union([shapely.force_2d(shape(f["geometry"])).buffer(0) for f in feats])
        in_any = set(np.nonzero(contains_xy(allb, SHOPS["pts"][:, 0], SHOPS["pts"][:, 1]))[0].tolist())
    extent = box(X0, Y0, X1, Y1)
    footprint = []
    forum = []
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
            if key in GF_PARTS:
                forum.append(poly)
                continue
            if key == NEUEHOUSE:
                footprint.append(neuehouse(m, poly, mntR))
                stats["landmarks"] = stats.get("landmarks", 0) + 1
                continue
            if key == CHURCH_SD:
                body = church_sainte_devote(m, mntR)
                footprint.append(body)
                stats["landmarks"] = stats.get("landmarks", 0) + 1
                rest = [q for q in to_polys(poly.difference(body.buffer(0.4, join_style=2))) if q.area >= 10]
                if not rest:
                    continue
                poly = orient(max(rest, key=lambda q: q.area), 1.0)
            if key in (STADE, STADE_TRIB) and STADE_POLY is not None:
                # Stade Louis II: built whole from both outlines, once.
                if not STADE_DONE:
                    STADE_DONE.append(1)
                    footprint.append(stade_louis_ii(m, mns, mntR, photo, STADE_POLY, stats))
                continue
            if key in WQ_DROP and WQ is not None:
                continue            # Cap d'Ail's raised walk: wq_build() lays it as a walk, not a block
            if key in DIGUE_PARTS:
                # The breakwater's terminal and raised walk: digue() builds
                # the whole thing (main), the mask takes its plan once.
                if not DIGUE_DONE:
                    DIGUE_DONE.append(1)
                    footprint.append(digue_footprint())
                continue
            if key == CHAPITEAU:
                footprint.append(chapiteau(m, mntR, stats))
                continue
            if key == JUSTICE:
                footprint.append(palais_justice(m, mntR, stats))
                stats["landmarks"] = stats.get("landmarks", 0) + 1
                continue
            if key in (PALACE, MUSEE, CATH, FORT, PRISON):
                # Le Rocher's landmarks are built piece by piece.
                if key == PRISON:
                    prison(m, poly, mntR, stats)
                else:
                    {PALACE: palais_princier, MUSEE: musee_oceanographique, CATH: cathedrale,
                     FORT: fort_antoine}[key](m, None, mntR, stats)
                footprint.append(poly)
                stats["landmarks"] = stats.get("landmarks", 0) + 1
                continue
            if key == HERACLES:
                # Palais Héraclès: the slab is built on its own; what is
                # left of the shared outline (the low wing to the north)
                # carries on below, at most a storey high.
                body = palais_heracles(m, mntR, stats)
                footprint.append(body)
                rest = [q for q in to_polys(poly.difference(body.buffer(1.0, join_style=2))) if q.area >= 10]
                if not rest:
                    continue
                poly = orient(max(rest, key=lambda q: q.area), 1.0)
            if key in HERACLES_WINGS:
                # The blocks either side of the Palais Héraclès.
                footprint.append(heracles_wing(m, poly, key, mntR, stats))
                stats["buildings"] += 1
                continue
            if key == HERACLES_COURT:
                continue            # its entrance court is built with the slab
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
            if key in CDP_VERANDA or key == HDP_VERANDA:
                # The Café de Paris's and the Hôtel de Paris bar's glass
                # verandas on the square: iron and lit glass, not a block.
                glass_veranda(m, poly, mntR, 5.5 if key == HDP_VERANDA else 3.8, stats)
                footprint.append(poly)
                continue
            if key not in (MUSEE, PRISON, PALACE, CATH, FORT) and box(*ROCHER_FINE_BOX).contains(poly.centroid):
                poly = rocher_cliff_top(poly, mntR)
                if poly is None:
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
            if key == HERACLES:
                hrel = np.minimum(hrel, 4.5)
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
            hero = HEROES.get(props.get("cleabs"))
            if hero:
                rk, tiled = "copper", False
                rcol = np.array(hexrgb(hero["roof"]))
                # The Casino's roof is a terrace of copper mansards with the
                # gaming halls' glazed roofs rising out of it (casino_halls):
                # walls to the terrace, the halls added from the LiDAR.
                levels = [(poly, float(np.percentile(hrel, 45)))]
            H = levels[-1][1]
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
            if props.get("cleabs") == HDP_MAIN:
                # Hôtel de Paris: white carved stone, a slate mansard.
                style, kind, grand = S_BELLE, STYLE_KIND[S_BELLE], True
                col = np.array(hexrgb("#eee8dc"))
            elif props.get("cleabs") == CDP_MAIN:
                # Café de Paris: cream-gold, arched ground floor (boutiques
                # on the Allée side).
                style, kind, grand = S_ARCH, STYLE_KIND[S_ARCH], True
                col = np.array(hexrgb("#e7cf9d"))
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
            if key in HELI_PLAIN:
                # The heliport's hangar and the white halls at the apron's
                # ends: plain cladding, no rows of windows.
                kind, style, grand = PLAIN, None, False
                col = np.array(hexrgb(HELI_PLAIN[key]), float)
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
            SHOP_NOW = shops_for(poly, SHOPS, in_any)
            if SHOP_NOW is None and ALLEE_FRONT is not None and poly.distance(ALLEE_FRONT) < ALLEE_HALF + 2.0:
                SHOP_NOW = {"pts": [], "areas": []}       # the Allée's boutique row
            if SHOP_NOW is not None:
                stats["shop_buildings"] = stats.get("shop_buildings", 0) + 1
            # Le Rocher's old town: mostly white and cream louvred shutters
            # (2019 street photos), a few green ones.
            PALE_NOW = 1.0 if ROCHER_ZONE.contains(poly.centroid) and rng_for(key + "pale").random() < 0.75 else 0.0
            zb = base - 1.0
            ARCADE_NOW = None
            if ARMES is not None and key in ARCADES:
                # The arcade on Place d'Armes: arches, shops behind, then
                # the walls on that side start above it.
                armes_arcade(m, poly, key, col, kind, style, fh, mntR, stats)
            for li, (region, hz) in enumerate(levels):
                zt = base + hz
                last = li == len(levels) - 1
                ph = None
                if photo is not None and not hero and not post_scan and key != FAIRMONT:
                    lx, ly = photo["lean"]
                    ph = dict(photo, shift=(hz * lx + adj[0], hz * ly + adj[1]))
                parts_ = tunnel_split(m, region, zb, zt, base, fh, col, kind, style, rcol)
                # Split over the tunnel: one parapet round the whole roof,
                # not one round each part (across the Fairmont's roof
                # garden the parts' shared edge stood up as a ridge
                # tracing the tunnel underneath).
                split_ = TUNNEL is not None and not (len(parts_) == 1 and parts_[0].equals(region))
                parapet_done = False
                for part in parts_:
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
                        if LIDAR_ROOFS and ph is not None and part.area > 300 and lidar_roof(
                                m, ov, eave, min(base + top + 1.0, eave + 7.0), mns, ph, np.asarray(col) * 0.95):
                            # A big tiled block is many roofs: their real
                            # shape from the LiDAR, not one hipped lid.
                            stats["lidar_roofs"] = stats.get("lidar_roofs", 0) + 1
                        else:
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
                        # A block over many tiled roofs (the photo shows
                        # them red): walls to the eaves, then the roofs'
                        # real shape from the LiDAR.
                        rs_ = red_share(part, ortho, ph["shift"]) if LIDAR_ROOFS and ph is not None and part.area > 60 else 0.0
                        if LIDAR_ROOFS and ph is not None and last and part.area > 60 and rs_ > 0.3:
                            ring_ = part.exterior
                            ss_ = shapely.line_interpolate_point(part.buffer(-0.8, join_style=2).exterior if isinstance(
                                part.buffer(-0.8, join_style=2), Polygon) else ring_, np.linspace(0, 1, 60), normalized=True)
                            ze_ = Raster(mns).sample(shapely.get_x(ss_), shapely.get_y(ss_))
                            eave_ = float(np.nanpercentile(ze_, 35)) if np.isfinite(ze_).any() else zt
                            eave_ = min(max(eave_, zb + 2.5), zt)
                            add_walls(m, part, zb, eave_, base, fh, col, kind, style=style)
                            if lidar_roof(m, part, eave_, min(zt + 3.0, eave_ + 7.0), mns, ph, np.asarray(col) * 0.95):
                                stats["lidar_roofs"] = stats.get("lidar_roofs", 0) + 1
                                continue
                            add_walls(m, part, eave_, zt, base, fh, col, kind, style=style)
                        else:
                            add_walls(m, part, zb, zt, base, fh, col, kind, style=style)
                        add_roof(m, part, zt, rcol, False, photo=ph)
                        if part.area > 40 and not parapet_done:
                            # Parapet: a low wall round the roof edge, so
                            # roofs read as terraces instead of lids.
                            pc = np.asarray(col) * 0.97
                            if style == S_BELLE:
                                # Belle Époque roofs end in a pale stone balustrade.
                                pc = np.array(hexrgb("#f1ebdf"))
                            for pp in ([orient(q_, 1.0) for q_ in to_polys(region) if q_.area > 40] if split_ else [part]):
                                add_walls(m, pp, zt, zt + 0.9, zt, 10, pc, PLAIN)
                                add_walls(m, pp, zt, zt + 0.9, zt, 10, pc * 0.85, PLAIN, inward=True)
                                inner_top = pp.buffer(-0.25, join_style=2)
                                ring_ = pp.difference(inner_top) if not inner_top.is_empty else pp
                                for rp in to_polys(ring_):
                                    add_roof(m, rp, zt + 0.9, pc * 1.05, False)
                            parapet_done = split_
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
            if hero:
                stats["casino_halls"] = stats.get("casino_halls", 0) + casino_halls(
                    m, poly, base, H, fh, col, kind, style, rcol, mns, stats)
            elif grand and poly.area > 300:
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
            if props.get("cleabs") == HDP_MAIN:
                hotel_de_paris(m, poly, base, H, mns, mntR, stats)
            if props.get("cleabs") == CASINO_FRONT:
                casino_front(m, poly, base, H, fh, col, kind, style, rcol, mntR)
            elif mt is not None:
                stats["mareterra_slabs"] = stats.get("mareterra_slabs", 0) + mareterra_terraces(
                    m, poly, base, H, fh, mt)
            elif not hero and not post_scan and (not tiled or info["balconies"]) and key not in HELI_PLAIN:
                stats["balconies"] = stats.get("balconies", 0) + facade_depth(
                    m, levels[0][0], base, hwall if tiled else levels[0][1], fh, style, col, rnd,
                    info["balconies"], info["depth"])
            if key == HELI_HANGAR:
                stats["hangar_doors"] = heli_hangar_doors(m, poly, base, H)
            if key == FAIRMONT:
                global FAIR
                FAIR = {"base": base, "fh": fh, "levels": [(r_, base + h_) for r_, h_ in levels],
                        "kind": kind, "style": style, "col": col}
            SHOP_NOW = None
            ARCADE_NOW = None
            PALE_NOW = 0.0
            footprint.append(poly)
            stats["buildings"] += 1
            stats["levels"] += len(levels)
    if forum:
        footprint += grimaldi_forum(m, forum, mns, mntR, photo, stats)
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
PORT_BOX = (7.4205, 43.7325, 7.4300, 43.7372)     # (to the Quai Louis II, east of the Yacht Club)


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
    osm = [(x, y) for (x, y), t in pts if t.get("natural") == "tree" and upper(x, y)]
    trunk = quai_pine_row(osm, upper, blds)
    planter, soil = np.array(hexrgb("#e6dfcf")), np.array(hexrgb("#5d4a3a"))
    for x, y, ax in trunk:
        z = float(QA["zup"](x, y)[0])
        # Square to the boulevard.
        _box(m, x, y, ax, 2.3, 2.3, z - 0.3, z + 0.5, planter)
        _box(m, x, y, ax, 1.8, 1.8, z + 0.5, z + 0.52, soil)
        h = float(rnd.uniform(11.5, 15.0))
        r = float(rnd.uniform(4.2, 5.4))
        add_tree(tm, x, y, z + 0.5, h, r, "pine", rnd)
        stats["trees"] += 1
    stats["quai_pines"] = len(trunk)
    return len(trunk)


QA_PINE_STEP = 11.0      # m between the pines along the boulevard (median OSM gap, street photos)
QA_PINE_OFFSET = 9.0     # m from the lap's centre line: just behind the boulevard's kerb


def quai_pine_row(osm, upper, blds):
    """The stone pines along Boulevard Albert 1er stand in one straight,
    evenly spaced row of planters just behind the kerb (street photos
    2021-2025). OpenStreetMap maps them 7-11 m from the road with gaps of
    4 to 38 m, so the trees only give the row's two ends: the planters
    are laid out at an even step between them, all at the same distance
    from the lap's centre line, each square to the road. Returns
    (x, y, axis) for each planter."""
    if len(osm) < 2:
        return []
    line = TRACK["line"]
    L = line.length
    O = np.array(osm, float)
    s_ = np.array([line.project(Point(*q)) for q in O])
    s_ = np.where(s_ > L / 2, s_ - L, s_)            # the row crosses the start line (s = 0)

    def frame(s):
        p, q = line.interpolate(s % L), line.interpolate((s + 3.0) % L)
        p0 = line.interpolate((s - 3.0) % L)
        t = np.array([q.x - p0.x, q.y - p0.y])
        t /= max(np.hypot(*t), 1e-9)
        return np.array([p.x, p.y]), t, np.array([-t[1], t[0]])
    # Which side of the road: the OSM trees' own side.
    side = np.sign(np.median([np.dot(q - frame(s)[0], frame(s)[2]) for q, s in zip(O, s_)]))
    s0, s1 = float(s_.min()), float(s_.max())
    n = max(1, int(round((s1 - s0) / QA_PINE_STEP)))
    out = []
    blds_ = blds if blds is not None and not blds.is_empty else None
    for s in np.linspace(s0, s1, n + 1):
        c, t, nrm = frame(s)
        q = c + nrm * side * QA_PINE_OFFSET
        if not upper(*q) or (blds_ is not None and blds_.buffer(1.5).contains(Point(*q))):
            continue
        out.append((float(q[0]), float(q[1]), t))
    return out


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


def railing(m, A, B, z, col, h=1.1, zb=None):
    """See-through railings from A to B (k x 2) standing on z (k,) (or z at
    A and zb at B, so a sloping railing has no steps): both faces, the bars
    drawn by the shader (PLAIN style RAIL)."""
    if not len(A):
        return 0.0
    L = np.linalg.norm(B - A, axis=1)
    s0 = np.r_[0.0, np.cumsum(L)[:-1]]
    s1 = s0 + L
    u = (B - A) / np.maximum(L, 1e-6)[:, None]
    nrm = np.column_stack([u[:, 1], -u[:, 0], np.zeros(len(A))])
    z = np.asarray(z, float)
    za = z if z.ndim else np.full(len(A), float(z))
    zb = za if zb is None else np.asarray(zb, float)
    E = lambda p, zz: enu_fast(p[:, 0], p[:, 1], zz)
    uv = [np.column_stack([s0, np.zeros(len(A))]), np.column_stack([s1, np.zeros(len(A))]),
          np.column_stack([s1, np.ones(len(A))]), np.column_stack([s0, np.ones(len(A))])]
    for sg in (1.0, -1.0):
        _quad_uv(m, E(A, za), E(B, zb), E(B, zb + h), E(A, za + h), np.array(hexrgb(col)), nrm * sg, uv, style=RAIL)
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
                or t.get("layer", "0") not in ("0",) or t.get("bridge") or len(p_) < 2 or is_carpark_way(p_) \
                or rocher_paved(t, p_):
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
    if CONDA is not None:
        corridor = corridor.union(CONDA["zone"])   # every street of La Condamine
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
    if ARMES is not None:
        other.append(ARMES["poly"].buffer(0.3))
    if CONDA is not None:
        other.append(CONDA["ped_poly"])
        if CONDA["park"] is not None:
            other.append(CONDA["park"])
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
    global HP
    hp = HP = hairpin_island(blds, mnt)
    if hp is not None:
        walk = unary_union([walk.difference(hp["poly"].buffer(1.0)), hp["upper"], hp["lower"]])
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
            # From Ostende to the tunnel a deep drop's foot can be further
            # out: beside the Mirabeau tower the ground falls 12 m within
            # 6 m of the kerb, and a wall stopping at the 3 m mark left
            # the rest as a pale smeared slope.
            far = np.stack([R.sample(mid[:, 0] + out[:, 0] * d, mid[:, 1] + out[:, 1] * d) for d in (4.0, 5.0, 6.0)])
            far = np.nanmin(np.where(np.isfinite(far), far, np.inf), 0)
            deep = down & in_upper(mid[:, 0], mid[:, 1]) & np.isfinite(far) & (low < zw - 3.0) & (far < low - 1.0)
            low = np.where(deep, far, low)
            # No single-metre walls or gaps.
            up = ndimage.binary_opening(ndimage.binary_closing(up, iterations=1), iterations=1)
            down = ndimage.binary_opening(ndimage.binary_closing(down, iterations=1), iterations=1) & ~up
            top = ndimage.median_filter(np.minimum(top, zw + 12.0), 5, mode="nearest")
            # (Beside the Mirabeau tower the street drops 12 m to the
            # tower's terrace: a wall stopping at 10 m left the rest to the
            # ground grid's smeared slope.)
            low = ndimage.minimum_filter1d(np.maximum(low, zw - 14.0), 3, mode="nearest")
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
    if TRACK is not None:
        # Never on the circuit itself: an edge of the pavement polygon can
        # cut a corner across the lap (by the Casino a 10 cm shift in the
        # polygon stood the ground on a wall's top on the road).
        mid = (w["a"] + w["b"]) / 2
        on = (track_dist(mid[:, 0], mid[:, 1]) < TRACK_HALF - 0.2) | \
             (track_dist(w["a"][:, 0], w["a"][:, 1]) < TRACK_HALF - 0.2) | \
             (track_dist(w["b"][:, 0], w["b"][:, 1]) < TRACK_HALF - 0.2)
        # (nor the level ground laid on top of a retaining wall, behind it)
        for e_ in ("a", "b"):
            q_ = w[e_] + w["out"] * SW_CAP
            on |= w["up"] & (track_dist(q_[:, 0], q_[:, 1]) < TRACK_HALF - 0.2)
        w = {k_: v[~on] for k_, v in w.items()}
    seg = np.linalg.norm(w["b"] - w["a"], axis=1)
    w["up_m"], w["down_m"] = float(seg[w["up"]].sum()), float(seg[~w["up"]].sum())
    return w


def wall_ground(X, Y, Zm):
    """Ground nodes just behind a retaining wall go down to the street, so
    the 3 m grid's slope stays behind the wall (under its cap) instead of
    rising in front of it. And round a drop, the nodes a grid step either
    side of the wall go down to its foot, so the slope is under the
    pavement, behind the wall, instead of a pale smear in front of it."""
    w = SW["walls"] if SW is not None else None
    if w is None or not len(w["a"]):
        return Zm
    from scipy.spatial import cKDTree
    Zm = drop_ground(X, Y, Zm, w)
    if not w["up"].any():
        return Zm
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


def drop_ground(X, Y, Zm, w):
    """Down to the foot of each tall drop wall, the ground nodes within a
    grid step of it on both sides. Beside the Mirabeau tower the street
    drops 12 m to the tower's terrace, and the 3 m grid ran from the
    road's edge down to the terrace as steep pale slopes in front of the
    wall: from the hairpin it looked like a hole beside the road. The
    nodes on the street side are under the pavement slab (its own mesh),
    those on the circuit keep the track's rules."""
    from scipy.spatial import cKDTree
    dn = ~w["up"] & ((w["zw"] - w["low"]) > 1.5)
    if not dn.any():
        return Zm
    m_ = (w["a"][dn] + w["b"][dn]) / 2
    o_ = w["out"][dn]
    lo = w["low"][dn]
    # Two grid steps out past the deep ones (more than 4 m down).
    reach = np.where(w["zw"][dn] - lo > 4.0, 2 * GRID + 0.6, GRID + 0.6)
    xr, yr = X.ravel(), Y.ravel()
    d, j = cKDTree(m_).query(np.column_stack([xr, yr]), distance_upper_bound=2 * GRID + 0.6)
    hit = np.isfinite(d)
    jj = np.where(hit, j, 0)
    hit &= d <= reach[jj]
    t = (xr - m_[jj, 0]) * o_[jj, 0] + (yr - m_[jj, 1]) * o_[jj, 1]
    sel = hit & (t > -(GRID + 0.3))
    # Street side: only under the pavement slab.
    ins = sel & (t < 0.3)
    if ins.any():
        ins[ins] = contains_xy(SW["poly"], xr[ins], yr[ins])
    sel &= (t >= 0.3) | ins
    if TRACK is not None:
        sel &= track_dist(xr, yr) > TRACK_HALF + 0.2
    sel = sel.reshape(X.shape)
    if sel.any():
        Zm = Zm.copy()
        Zm[sel] = np.minimum(Zm[sel], lo[jj.reshape(X.shape)[sel]] - 0.1)
    return Zm


BALUSTRADE_R = 280.0                   # m from the Casino fountain: stone balustrades
BALUSTER = "#ece6d8"


def balustrade(m, A, B, Z):
    """A stone balustrade along A-B on Z: a plinth, turned balusters
    every 0.28 m (a square foot, a round belly, a square neck), a pier
    every few metres and a moulded rail."""
    col = np.array(hexrgb(BALUSTER))
    tot = 0.0
    for a, b, z in zip(A, B, Z):
        L = float(np.linalg.norm(b - a))
        if L < 0.3:
            continue
        u = (b - a) / L
        c = (a + b) / 2
        _box(m, c[0], c[1], u, L + 0.02, 0.36, z, z + 0.16, col * 0.95)                  # plinth
        _box(m, c[0], c[1], u, L + 0.02, 0.38, z + 0.82, z + 0.95, col)                  # rail
        for t in np.arange(0.14, L, 0.28):
            p = a + u * t
            _box(m, p[0], p[1], u, 0.11, 0.11, z + 0.16, z + 0.82, col * 0.97, top=False)
            _box(m, p[0], p[1], u, 0.17, 0.17, z + 0.3, z + 0.52, col, top=False)        # the belly
        tot += L
    # A pier about every 3 m.
    run = 3.0
    for a, b, z in zip(A, B, Z):
        run += float(np.linalg.norm(b - a))
        if run >= 3.0:
            run = 0.0
            _box(m, a[0], a[1], b - a, 0.34, 0.4, z, z + 1.02, col * 0.98)
    return tot


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
            # Round the Casino the gardens end in a white stone balustrade
            # (small turned columns under a rail), elsewhere iron railings.
            mid_ = (A[g_] + B[g_]) / 2
            bal = np.hypot(mid_[:, 0] - CASINO_SQUARE[0], mid_[:, 1] - CASINO_SQUARE[1]) < BALUSTRADE_R
            if (~bal).any():
                rails += railing(m, (A[g_] + O[g_, :2] * 0.15)[~bal], (B[g_] + O[g_, :2] * 0.15)[~bal],
                                 Z1[g_][~bal] + 0.06, GARDEN_RAIL, 1.2)
            if bal.any():
                n_b = balustrade(m, (A[g_] + O[g_, :2] * 0.2)[bal], (B[g_] + O[g_, :2] * 0.2)[bal], Z1[g_][bal] + 0.04)
                stats["balustrade_m"] = stats.get("balustrade_m", 0) + int(n_b)
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
        # Tall drops from Ostende to the tunnel: the rubble limestone of
        # the Mirabeau retaining walls (a plain pale face read as a gap).
        rb = tall & ud
        pl = ~rb
        if pl.any():
            _quad(m, E(Ao[pl], Z0[pl]), E(Bo[pl], Z0[pl]), E(Bo[pl], Zt[pl]), E(Ao[pl], Zt[pl]),
                  stone[dn][pl] * 0.95, O[pl])
        if rb.any():
            S0, S1 = s0[dn][rb], s1[dn][rb]
            hh = Zt[rb] - Z0[rb]
            rc = np.array(hexrgb(WALL_RUBBLE))[None] * tint[dn][rb]
            _quad_uv(m, E(Ao[rb], Z0[rb]), E(Bo[rb], Z0[rb]), E(Bo[rb], Zt[rb]), E(Ao[rb], Zt[rb]), rc, O[rb],
                     [np.column_stack([S0, np.zeros(rb.sum())]), np.column_stack([S1, np.zeros(rb.sum())]),
                      np.column_stack([S1, hh]), np.column_stack([S0, hh])], style=RUBBLE)
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


HAIRPIN = (1100.0, 1265.0, 1400.0)     # lap metres: the Fairmont hairpin's way in, apex, way out
HAIRPIN_GAP = 22.0                     # m: the island is where both legs are this close
HP = None


def hairpin_island(blds, mnt=None):
    """The island inside the Fairmont hairpin, between its two legs, all
    the way back to where they part (buildings cut out), at the way in's
    level. The way in (Avenue des Spélugues) really is the higher leg: the
    LiDAR has the way out (Avenue Princesse Grace) 3 m lower 30 m from the
    apex and 8 to 11 m lower where the two part, behind a vertical
    retaining wall at the edge of the way in. The island stops at the way
    out's kerb, where sidewalks() stands that wall. (It used to stop 45 m
    from the apex: past it the 3 m ground grid hung over the 11 m drop as
    one long pale slope ahead of the cars on the way out.)"""
    if TRACK is None:
        return None
    t = TRACK
    st = t["step"]
    idx = np.arange(int(HAIRPIN[0] / st), int(HAIRPIN[2] / st))
    loop = Polygon(np.column_stack([t["x"][idx], t["y"][idx]])).buffer(0)
    ia = int(HAIRPIN[1] / st)
    i_in = np.arange(int(HAIRPIN[0] / st), ia + 1)
    i_out = np.arange(ia, int(HAIRPIN[2] / st))
    leg_in = LineString(np.column_stack([t["x"][i_in], t["y"][i_in]]))
    leg_out = LineString(np.column_stack([t["x"][i_out], t["y"][i_out]]))
    both = leg_in.buffer(HAIRPIN_GAP).intersection(leg_out.buffer(HAIRPIN_GAP))
    isl = loop.intersection(both).difference(t["line"].buffer(SW_START))
    if not blds.is_empty:
        isl = isl.difference(blds)
    parts = [q for q in to_polys(isl) if q.area > 8]
    if not parts:
        return None
    isl = max(parts, key=lambda q: q.area)
    from scipy.spatial import cKDTree
    kd_in = cKDTree(np.column_stack([t["x"][i_in], t["y"][i_in]]))
    kd_out = cKDTree(np.column_stack([t["x"][i_out], t["y"][i_out]]))
    z_in, z_out = t["z"][i_in], t["z"][i_out]

    def zi(x, y):
        _, j = kd_in.query(np.column_stack([np.atleast_1d(x), np.atleast_1d(y)]))
        return z_in[j]

    def zo(x, y):
        _, j = kd_out.query(np.column_stack([np.atleast_1d(x), np.atleast_1d(y)]))
        return z_out[j]
    # Which part is up at the way in's level and which down at the way
    # out's. The wall runs parallel to the way in, at one distance past
    # its kerb (the LiDAR's: where the ground first drops 1.5 m below the
    # road, median along the stretch), from where the legs are 1.2 m apart
    # in height on back. (Following the LiDAR's own outline half a metre
    # at a time made the wall and its railing zigzag.)
    upper = isl
    lower = Polygon()
    if mnt is not None:
        R = Raster(mnt)
        xi, yi = t["x"][i_in], t["y"][i_in]
        dx, dy = np.gradient(xi), np.gradient(yi)
        ln = np.hypot(dx, dy) + 1e-9
        nx_, ny_ = -dy / ln, dx / ln
        probe = np.column_stack([xi + nx_ * 8.0, yi + ny_ * 8.0])
        sgn = np.where(contains_xy(loop, probe[:, 0], probe[:, 1]), 1.0, -1.0)
        nx_, ny_ = nx_ * sgn, ny_ * sgn
        dz = z_in - zo(xi, yi)
        drops = []
        for k in np.nonzero(dz > 2.0)[0]:
            for d in np.arange(SW_START, SW_START + 6.0, 0.25):
                g = R.sample(np.array([xi[k] + nx_[k] * d]), np.array([yi[k] + ny_[k] * d]))[0]
                if np.isfinite(g) and g < z_in[k] - 1.5:
                    drops.append(d - SW_START)
                    break
        w = float(np.clip(np.median(drops), 1.0, 2.5)) if drops else 1.2
        # Where the wall ends: the last point back from the apex with the
        # legs within 1.2 m of each other in height.
        k_cut = np.nonzero(dz > 1.2)[0]
        if len(k_cut):
            kc = int(k_cut.max())
            u = np.array([dx[kc], dy[kc]]) / ln[kc]
            c = np.array([xi[kc], yi[kc]])
            # Everything on the way in's start side of the cut line.
            half = Polygon([c + 60 * np.array([-u[1], u[0]]), c + 60 * np.array([-u[1], u[0]]) - 200 * u,
                            c - 60 * np.array([-u[1], u[0]]) - 200 * u, c - 60 * np.array([-u[1], u[0]])])
            wall_line = leg_in.buffer(SW_START + w)
            lp = isl.difference(wall_line).intersection(half)
            lower = unary_union([q for q in to_polys(lp) if q.area > 3])
            if not lower.is_empty:
                upper = unary_union([q for q in to_polys(isl.difference(lower.buffer(0.05))) if q.area > 3])
        print(f"Fairmont hairpin wall {w:.1f} m past the way in's kerb, from lap {i_in[kc] * st if len(k_cut) else 0:.0f} m")
    lower = unary_union([q for q in to_polys(lower) if q.area > 3]) if not lower.is_empty else lower
    up_zone = upper.buffer(0.03) if not upper.is_empty else upper

    def z(x, y):
        x, y = np.atleast_1d(x), np.atleast_1d(y)
        if up_zone.is_empty:
            return zo(x, y)
        return np.where(contains_xy(up_zone, x, y), zi(x, y), np.minimum(zo(x, y), zi(x, y)))
    print(f"Fairmont hairpin island: {isl.area:.0f} m2, {upper.area:.0f} at the way in's level, "
          f"{lower.area:.0f} down at the way out's")
    return {"poly": isl, "upper": upper, "lower": lower, "z": z, "z_in": zi, "z_out": zo}


def hairpin_wall(m):
    """The retaining wall inside the Fairmont hairpin: from the lower part
    of the island (at the way out's level) up to the higher part (the way
    in's), in rubble limestone like the walls on the Mirabeau descent, a
    white railing on top."""
    hp = HP
    if hp is None or hp["lower"].is_empty or hp["upper"].is_empty:
        return 0.0
    near = hp["lower"].buffer(0.3)
    A, B_, OUT = [], [], []
    for pg in to_polys(hp["upper"]):
        for ring in [pg.exterior] + list(pg.interiors):
            L = ring.length
            ss = np.linspace(0, L, max(2, int(np.ceil(L / 0.8))) + 1)
            q = shapely.line_interpolate_point(ring, ss)
            P = np.column_stack([shapely.get_x(q), shapely.get_y(q)])
            a, b = P[:-1], P[1:]
            u = b - a
            ln = np.linalg.norm(u, axis=1)
            ok = ln > 0.05
            a, b, u, ln = a[ok], b[ok], u[ok], ln[ok]
            u = u / ln[:, None]
            out = np.column_stack([u[:, 1], -u[:, 0]])
            mid = (a + b) / 2
            out[contains_xy(pg, mid[:, 0] + out[:, 0] * 0.15, mid[:, 1] + out[:, 1] * 0.15)] *= -1
            k = contains_xy(near, mid[:, 0] + out[:, 0] * 0.25, mid[:, 1] + out[:, 1] * 0.25)
            A.append(a[k]); B_.append(b[k]); OUT.append(out[k])
    a, b, out = np.concatenate(A), np.concatenate(B_), np.concatenate(OUT)
    if not len(a):
        return 0.0
    zt_a, zt_b = hp["z_in"](a[:, 0], a[:, 1]) + SW_KERB, hp["z_in"](b[:, 0], b[:, 1]) + SW_KERB
    zl_a = hp["z_out"](a[:, 0] + out[:, 0], a[:, 1] + out[:, 1]) - 0.3
    zl_b = hp["z_out"](b[:, 0] + out[:, 0], b[:, 1] + out[:, 1]) - 0.3
    zl_a, zl_b = np.minimum(zl_a, zt_a - 0.3), np.minimum(zl_b, zt_b - 0.3)
    c1 = np.cumsum(np.linalg.norm(b - a, axis=1))
    c0 = np.r_[0.0, c1[:-1]]
    o3 = np.column_stack([out, np.zeros(len(a))])
    _quad_uv(m, enu_fast(a[:, 0], a[:, 1], zl_a), enu_fast(b[:, 0], b[:, 1], zl_b),
             enu_fast(b[:, 0], b[:, 1], zt_b + 0.08), enu_fast(a[:, 0], a[:, 1], zt_a + 0.08),
             np.array(hexrgb(WALL_RUBBLE)), o3,
             [np.column_stack([c0, np.zeros(len(a))]), np.column_stack([c1, np.zeros(len(a))]),
              np.column_stack([c1, zt_b + 0.08 - zl_b]), np.column_stack([c0, zt_a + 0.08 - zl_a])], style=RUBBLE)
    # A coping stone on top, then the railing where the drop is over a metre.
    ins = -out * 0.3
    _quad(m, enu_fast(a[:, 0], a[:, 1], zt_a + 0.08), enu_fast(b[:, 0], b[:, 1], zt_b + 0.08),
          enu_fast(b[:, 0] + ins[:, 0], b[:, 1] + ins[:, 1], zt_b + 0.08),
          enu_fast(a[:, 0] + ins[:, 0], a[:, 1] + ins[:, 1], zt_a + 0.08),
          np.array(hexrgb(KERB_GRANITE)), np.array([[0.0, 0.0, 1.0]]))
    rt = (np.minimum(zt_a - zl_a, zt_b - zl_b) > 1.3)
    if rt.any():
        railing(m, (a - out * 0.15)[rt], (b - out * 0.15)[rt], zt_a[rt] + 0.08, STREET_RAIL, 1.0, zb=zt_b[rt] + 0.08)
    print(f"Fairmont hairpin retaining wall: {c1[-1]:.0f} m, up to {np.max(zt_a - zl_a):.1f} m high")
    return float(c1[-1])


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
    if HP is not None and not HP["lower"].is_empty:
        # Inside the Fairmont hairpin the 3 m ground grid ran from the way
        # in straight down to the way out as one steep slope, standing out
        # in front of the retaining wall: every node off the road within a
        # grid step of the lower part goes down to it (the wall hides the
        # rest). Under the way in's own road too (the road is its own
        # mesh at the lap's height): a node left up there made a cone of
        # ground in front of the wall with its lowered neighbours.
        nr = contains_xy(HP["lower"].buffer(GRID * 1.2), X, Y)
        if nr.any():
            zl = HP["z_out"](X[nr], Y[nr]) - 0.3
            off = track_dist(X[nr], Y[nr]) > TRACK_HALF + 0.2
            Zm = Zm.copy(); Zs = Zs.copy()
            Zm[nr] = np.minimum(Zm[nr], zl)
            zs = Zs[nr]
            zs[off] = np.minimum(zs[off], zl[off] + 0.16)
            Zs[nr] = zs
    return wall_ground(X, Y, Zm), Zs


# ------------------------------------------------- Allée François Blanc ----
# The pedestrian street between the Casino and the Café de Paris, running
# down from Casino square towards the Spélugues gardens: pale stone paving
# wall to wall, no kerbs, no asphalt, and the luxury boutiques (watchmakers
# and jewellers: Jaeger-LeCoultre, Hublot, Richard Mille, Vacheron
# Constantin, Chaumet, Tiffany, Messika...) in tall glazed bays framed in
# stone, each where OpenStreetMap maps the shop.
ALLEE_NAME = "Allée François Blanc"
ALLEE_HALF = 9.0                     # m either side of the mapped line to pave


def allee_line(ways):
    line = None
    for t, p_, closed in ways:
        if t.get("name") == ALLEE_NAME and t.get("highway") and len(p_) >= 2:
            g = LineString(p_)
            line = g if line is None or g.length > line.length else line
    return line


def allee_profile(line, mntR):
    """The Allée's level along its line: the median of the terrain across
    the middle 10 m, then a running median and mean, so the paving runs
    down to the gardens in one even slope (no 50 cm LiDAR humps and dips)."""
    ss = np.arange(0.0, line.length + 0.5, 1.0)
    pp = shapely.line_interpolate_point(line, ss)
    q2 = shapely.line_interpolate_point(line, np.minimum(ss + 0.5, line.length))
    q1 = shapely.line_interpolate_point(line, np.maximum(ss - 0.5, 0))
    d = np.column_stack([shapely.get_x(q2) - shapely.get_x(q1), shapely.get_y(q2) - shapely.get_y(q1)])
    d /= np.linalg.norm(d, axis=1, keepdims=True) + 1e-9
    nrm = np.column_stack([-d[:, 1], d[:, 0]])
    off = np.arange(-5.0, 5.1, 1.0)
    X = shapely.get_x(pp)[:, None] + nrm[:, :1] * off
    Y = shapely.get_y(pp)[:, None] + nrm[:, 1:] * off
    z = mntR.sample(X.ravel(), Y.ravel()).reshape(X.shape)
    zc = np.nanmedian(z, axis=1)
    ok = np.isfinite(zc)
    zc = np.interp(ss, ss[ok], zc[ok])
    zc = ndimage.median_filter(zc, size=7, mode="nearest")
    zc = ndimage.uniform_filter1d(zc, size=9, mode="nearest")
    return lambda x, y: np.interp(shapely.line_locate_point(line, shapely.points(x, y)), ss, zc)


def allee_francois_blanc(gm, m, ways, pts, mnt, fps, stats, gz=None):
    line = allee_line(ways)
    if line is None:
        return 0
    mntR = Raster(mnt)
    prof = allee_profile(line, mntR)
    near = [p for p in fps if p.distance(line) < ALLEE_HALF + 4]
    blds = unary_union(near) if near else Polygon()
    # Paving: the street between the walls, from the square to the gardens.
    area = line.buffer(ALLEE_HALF, cap_style=2).difference(blds.buffer(0.05))
    n_ = 0
    for q in to_polys(area):
        q = shapely.set_precision(q.simplify(0.05), 0.01)
        if q.is_empty or q.geom_type != "Polygon" or q.area < 2 or not q.is_valid:
            continue
        tr = triangulate(orient(q, 1.0), area=4)
        if tr is None:
            continue
        v, tri = tr
        z = prof(v[:, 0], v[:, 1])
        if gz is not None:
            # Above the 3 m ground mesh too (its triangles bulge over the
            # 50 cm terrain in places and showed through as dark patches).
            fi, fj = (v[:, 0] - X0) / GRID, (Y1 - v[:, 1]) / GRID
            z = np.maximum(z, ndimage.map_coordinates(gz, [fj, fi], order=1, mode="nearest"))
        zt = z[tri]
        tri = tri[(zt.max(1) - zt.min(1)) < 1.5]
        P = enu_fast(v[:, 0], v[:, 1], z + 0.1)
        uv = np.column_stack([(v[:, 0] - X0) / (X1 - X0), (Y1 - v[:, 1]) / (Y1 - Y0)])
        gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=tri, style=SLAB)
        n_ += 1
    # The boutiques themselves are drawn into the walls' own arches by the
    # shader (a real OSM shop behind the bay: glazed arch, fascia, lit).
    shops = [(x, y, t) for (x, y), t in pts if (t.get("shop") or t.get("brand")) and
             LineString(line.coords).distance(Point(x, y)) < ALLEE_HALF + 6]
    stats["allee_shops"] = len(shops)
    return n_


# Round the Casino: a pale stone apron along every wall (the terraces and
# the walks round the halls), not the photo's smeared garden green.
CASINO_APRON = 6.0


def casino_apron(gm, ways, fps, gz, stats):
    heroes = [p for p in fps if any(p.distance(Point(*_rel(e, n))) < 1.0 for e, n in CASINO_HALLS)]
    if not heroes or gz is None:
        return 0
    hb = unary_union(heroes)
    ring = hb.buffer(CASINO_APRON, join_style=2)
    near = [p for p in fps if p.intersects(ring)]
    cut = [unary_union(near).buffer(0.05)]
    rp, _ = _road_polys(ways)
    cut += [q.buffer(0.5) for q in rp if q.intersects(ring)]
    if TRACK is not None:
        cut.append(TRACK["line"].buffer(TRACK_HALF + 3.0))
    if SW is not None:
        cut.append(SW["poly"].buffer(0.05))
    for t, p_, closed in ways:
        if closed and len(p_) >= 4 and (t.get("landuse") == "grass" or t.get("natural") in ("grassland", "scrub")
                                        or t.get("leisure") == "garden" or t.get("landcover") == "grass"):
            g = Polygon(p_).buffer(0)
            if g.intersects(ring):
                cut.append(g)
    area = ring.difference(unary_union(cut))
    n_, a_ = 0, 0.0
    for q in to_polys(area):
        q = shapely.set_precision(q.simplify(0.05), 0.01)
        if q.is_empty or q.geom_type != "Polygon" or q.area < 3 or not q.is_valid:
            continue
        tr = triangulate(orient(q, 1.0), area=4)
        if tr is None:
            continue
        v, tri = tr
        fi, fj = (v[:, 0] - X0) / GRID, (Y1 - v[:, 1]) / GRID
        z = ndimage.map_coordinates(gz, [fj, fi], order=1, mode="nearest") + 0.1
        P = enu_fast(v[:, 0], v[:, 1], z)
        uv = np.column_stack([(v[:, 0] - X0) / (X1 - X0), (Y1 - v[:, 1]) / (Y1 - Y0)])
        gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=tri, style=SLAB)
        n_ += 1
        a_ += q.area
    stats["casino_apron_m2"] = round(a_)
    return n_


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
                    railing(m, (a - out * 0.12)[rt], (b - out * 0.12)[rt], za[rt] + 0.01, STREET_RAIL, 0.9, zb=zb[rt] + 0.01)
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
    stats["hairpin_wall_m"] = int(hairpin_wall(m))
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
                    # The underside where the building stands over the
                    # open side; fairmont() builds the quay and columns.
                    if "open_band" in TUNNEL and q.intersects(TUNNEL["open_band"]) and zb < top:
                        t_ = triangulate(q)
                        if t_ is not None:
                            v_, tri_ = t_
                            P_ = enu_fast(v_[:, 0], v_[:, 1], np.full(len(v_), top))
                            m.tri(P_[tri_[:, 0]], P_[tri_[:, 2]], P_[tri_[:, 1]], np.asarray(col) * 0.6, PLAIN)
                        OPEN_PARTS.append((q, top))
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
    stone_in, stone_out = np.array(hexrgb("#d8d4c8")), np.array(hexrgb("#a9a296"))   # outside: in the hotel's shade

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


# Bridges that run into a tunnel (Boulevard du Larvotto's viaduct goes under
# the hill by the Fairmont hairpin): the deck used to stop at a slope of
# smeared ground, a dead end. Each gets a portal: a headwall with the
# opening, the tunnel's first metres inside, dark at the back.
PORTALS = []
GROUND_Z = None
PORTAL_IN = 12.0                    # m of tunnel modelled inside the mouth


def portals_setup(ways, mnt):
    """Where an OpenStreetMap bridge's end meets a tunnel and the ground
    beyond rises well above the deck."""
    R = Raster(mnt)
    for t, pts, closed in ways:
        hw_ = t.get("highway")
        w = BRIDGE_W.get(hw_)
        if not w or not t.get("bridge") or closed or len(pts) < 2 or \
                hw_ in ("footway", "steps", "cycleway", "path", "pedestrian"):
            continue
        for e, pv in ((pts[0], pts[1]), (pts[-1], pts[-2])):
            E = Point(e)
            if not any(t2.get("tunnel") == "yes" and t2.get("highway") in BRIDGE_W and not cl2 and len(p2) > 1 and
                       min(Point(p2[0]).distance(E), Point(p2[-1]).distance(E)) < 0.8 for t2, p2, cl2 in ways):
                continue
            u = np.array(e, float) - np.array(pv, float)
            u /= max(np.linalg.norm(u), 1e-6)
            e = np.array(e, float)
            g_in = R.sample(np.array([e[0] + u[0] * 6.0]), np.array([e[1] + u[1] * 6.0]))[0]
            g_out = R.sample(np.array([e[0] - u[0] * 3.0]), np.array([e[1] - u[1] * 3.0]))[0]
            if not (np.isfinite(g_in) and np.isfinite(g_out)) or g_in < g_out + 4.0:
                continue
            if not (X0 + 30 < e[0] < X1 - 30 and Y0 + 30 < e[1] < Y1 - 30):
                continue
            l = np.array([-u[1], u[0]])
            hw = w / 2
            W = hw + 2.5

            def P_(a, b):
                return tuple(e + u * a + l * b)
            cut = Polygon([P_(-3.5, -W), P_(PORTAL_IN, -W), P_(PORTAL_IN, W), P_(-3.5, W)])
            if TRACK is not None and track_dist(*np.array(cut.exterior.coords).T).min() < TRACK_HALF + 3.5:
                continue
            PORTALS.append({"e": e, "u": u, "l": l, "hw": hw, "cut": cut, "name": t.get("name")})
    print("tunnel portals:", [p_["name"] for p_ in PORTALS])


def _grid_z(x, y):
    """The ground grid's height, bilinear."""
    Z = GROUND_Z
    fi = np.clip((Y1 - y) / GRID, 0, Z.shape[0] - 1.001)
    fj = np.clip((x - X0) / GRID, 0, Z.shape[1] - 1.001)
    i, j = np.floor(fi).astype(int), np.floor(fj).astype(int)
    ti, tj = fi - i, fj - j
    return (Z[i, j] * (1 - ti) * (1 - tj) + Z[i, j + 1] * (1 - ti) * tj +
            Z[i + 1, j] * ti * (1 - tj) + Z[i + 1, j + 1] * ti * tj)


def portals(gm, m, stats):
    """Each portal: the hole cut in the ground grid filled (low under the
    deck in front, the hill over the tunnel behind), a concrete headwall
    round the opening, the tunnel's walls, ceiling and road for its first
    metres, and the dark beyond."""
    n = 0
    concrete = np.array(hexrgb("#d6cfbf"))
    cope = np.array(hexrgb("#ece4d3"))
    wall_in, ceil, deep = np.array(hexrgb("#8c877c")), np.array(hexrgb("#4a4843")), np.array(hexrgb("#0b0b0c"))
    asph = np.array(hexrgb("#55585e"))
    for pt_ in PORTALS:
        if "zr" not in pt_ or "hole" not in pt_ or pt_["hole"].is_empty:
            continue
        e, u, l, hw, zr = pt_["e"], pt_["u"], pt_["l"], pt_["hw"], pt_["zr"]
        H = 5.0                                     # the opening's height
        z_roof = zr + H + 0.4
        # The filled hole: a half-plane each side of the headwall.
        big = 80.0
        front = Polygon([tuple(e + l * big), tuple(e - l * big), tuple(e - l * big - u * big), tuple(e + l * big - u * big)])
        back = Polygon([tuple(e + l * big), tuple(e - l * big), tuple(e - l * big + u * big), tuple(e + l * big + u * big)])
        hole = pt_["hole"]
        hf, hb = hole.intersection(front), hole.intersection(back)
        _flat_slab(gm, hf, lambda x, y: np.minimum(_grid_z(x, y), zr - 1.2))
        _flat_slab(gm, hb, lambda x, y: np.maximum(_grid_z(x, y), z_roof))
        # Lateral extent of the headwall: the hole's width at the mouth.
        cross = LineString([tuple(e - l * big), tuple(e + l * big)]).intersection(hole.buffer(0.01))
        if cross.is_empty:
            continue
        cc = np.array([c for g_ in getattr(cross, "geoms", [cross]) for c in g_.coords])
        off = (cc - e) @ l
        w0, w1 = float(off.min()), float(off.max())
        offs = np.linspace(w0, w1, 9)
        zg_top = float(np.max(np.maximum(_grid_z(e[0] + l[0] * offs + u[0] * 0.3, e[1] + l[1] * offs + u[1] * 0.3), z_roof)))
        z_bot = float(np.min(np.minimum(_grid_z(e[0] + l[0] * offs - u[0] * 0.3, e[1] + l[1] * offs - u[1] * 0.3),
                                        zr - 1.2))) - 0.5
        zt = zg_top + 0.9                            # a parapet over the mouth

        def E1(a, b, z):
            p = e + u * a + l * b
            return enu_fast(np.array([p[0]]), np.array([p[1]]), np.array([z]))
        o_out = E1(-1, 0, zr) - E1(0, 0, zr)
        o_in = -o_out
        # Headwall: the two piers beside the opening and the lintel over it.
        for b0, b1, za, zb_ in ((w0, -hw, z_bot, zt), (hw, w1, z_bot, zt), (-hw, hw, zr + H, zt)):
            if b1 - b0 < 0.05:
                continue
            _quad(m, E1(0, b0, za), E1(0, b1, za), E1(0, b1, zb_), E1(0, b0, zb_), concrete[None], o_out)
        # (its top: a coping 40 cm deep, and the back of the parapet)
        _quad(m, E1(-0.1, w0, zt), E1(-0.1, w1, zt), E1(0.4, w1, zt), E1(0.4, w0, zt), cope[None],
              E1(0, 0, zr + 1) - E1(0, 0, zr))
        _quad(m, E1(0.4, w0, zg_top), E1(0.4, w1, zg_top), E1(0.4, w1, zt), E1(0.4, w0, zt), concrete[None] * 0.95, o_in)
        # A band round the opening.
        for b0, b1, za, zb_ in ((-hw - 0.5, -hw, zr, zr + H + 0.5), (hw, hw + 0.5, zr, zr + H + 0.5),
                                (-hw, hw, zr + H, zr + H + 0.5)):
            _quad(m, E1(-0.15, b0, za), E1(-0.15, b1, za), E1(-0.15, b1, zb_), E1(-0.15, b0, zb_), cope[None] * 0.97, o_out)
        # Inside: road, walls, ceiling, and the dark.
        L = PORTAL_IN
        up_ = E1(0, 0, zr + 1) - E1(0, 0, zr)
        _quad(m, E1(0, -hw, zr + 0.03), E1(0, hw, zr + 0.03), E1(L, hw, zr + 0.03), E1(L, -hw, zr + 0.03), asph[None], up_)
        for sgn in (-1, 1):
            side_in = E1(0, 0, zr) - E1(0, sgn, zr)
            _quad(m, E1(0, sgn * hw, zr), E1(L, sgn * hw, zr), E1(L, sgn * hw, zr + H), E1(0, sgn * hw, zr + H),
                  wall_in[None], side_in)
        _quad(m, E1(0, -hw, zr + H), E1(0, hw, zr + H), E1(L, hw, zr + H), E1(L, -hw, zr + H), ceil[None], -up_)
        _quad(m, E1(L, -hw, zr - 0.1), E1(L, hw, zr - 0.1), E1(L, hw, zr + H), E1(L, -hw, zr + H), deep[None], o_out)
        # The tunnel's lid under the hill, so nothing shows through from above.
        _quad(m, E1(0, -hw, z_roof - 0.05), E1(0, hw, z_roof - 0.05), E1(L, hw, z_roof - 0.05), E1(L, -hw, z_roof - 0.05),
              ceil[None], up_)
        n += 1
    stats["tunnel_portals"] = n
    return n


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
        for pt_ in PORTALS:
            # The deck's height where it reaches a tunnel's mouth.
            d_ = np.hypot(x - pt_["e"][0], y - pt_["e"][1])
            if d_.min() < 1.6:
                pt_["zr"] = float(z[int(np.argmin(d_))])
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
        pal_ = {"olive": ["#8b9a70", "#82936a", "#94a27a"],       # silvery grey-green
                "orange": ["#2f5a2b", "#365f2f", "#2c5428"]}.get(kind, [
            "#5c7a3c", "#66813f", "#56733a", "#6d8446", "#5b7547", "#748450", "#61803f"])
        base_g = np.array(hexrgb(rnd.choice(pal_)))
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
        if kind == "orange":
            # Bitter oranges on the street trees of La Condamine.
            for _ in range(10):
                a_, e_ = rnd.uniform(0, 2 * math.pi), rnd.uniform(-0.4, 0.8)
                _ball(m, x + lean[0] + math.cos(a_) * math.cos(e_) * r * 0.95,
                      y + lean[1] + math.sin(a_) * math.cos(e_) * r * 0.95,
                      z + cz + math.sin(e_) * rz * 0.95, 0.09, np.array(hexrgb("#e8871e")), None, nu=6, nv=4)


def no_tree_zones(ways):
    """Pools, pool decks, sports grounds and piers: open paved places
    where the LiDAR sees diving boards, stands, awnings and masts standing
    up, but no trees grow."""
    out = []
    # Place du Palais: its pines come from rocher_trees().
    out.append(Frame(None, PALACE_O, PALACE_U).poly(-60.0, 68.0, 0.5, 70.0))
    if ROCHER_GARDENS is not None:
        out.append(ROCHER_GARDENS)                 # ... and the Rock's gardens' too
    # The Oceanographic Museum's sea face: the LiDAR's edge of the building
    # over the drop reads as a row of tall trees standing against it.
    out.append(Frame(None, MUSEE_O, MUSEE_U).poly(-3.0, MUSEE_L + 3.0, -0.5, 9.0))
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
    out.append(digue_zone())                       # digue() plants the abutment's walk
    if FM is not None:
        out += [c.buffer(1.0) for c in FM["courts"]]
    if TUNNEL is not None and "open_ground" in TUNNEL:
        out.append(TUNNEL["open_ground"])          # the Fairmont's quay and sea wall
    if ARMES is not None:
        out.append(ARMES["zone"])                  # armes_trees() plants the square's own
    if CONDA is not None:                          # condamine() plants these
        if CONDA["park"] is not None:
            out.append(CONDA["park"].buffer(1.0))
        out += [ln.buffer(CONDA_PED_W) for t, ln in CONDA["ped"] if t.get("name") in ORANGE_STREETS]
        out += [LineString(p).intersection(CONDA["zone"]).buffer(9.0) for t, p, c in ways
                if t.get("name") == "Rue Grimaldi" and t.get("highway") in ROAD_W and len(p) >= 2]
    al = allee_line(ways)
    if al is not None and al.length > 30:
        # The Allée François Blanc is open paving from the square down to
        # the gardens (the gardens' end keeps its trees).
        out.append(shapely.ops.substring(al, 22.0, al.length).buffer(5.0))
    return unary_union(out) if out else None


# Casino square (Place du Casino) is a paved plaza with a few tall palms and
# no other trees: the palms stand where OpenStreetMap maps them (round the
# fountain and on the Café de Paris terrace, as in the 2026 street photos).
# Corners in metres east / north of the Casino fountain.
CASINO_PALM_ZONE = [(-27, -24), (-30, 8), (-22, 30), (8, 38), (30, 30), (44, 2), (46, -8), (24, -44), (2, -42)]


def casino_palm_zone():
    return Polygon([(CASINO_SQUARE[0] + e, CASINO_SQUARE[1] + n) for e, n in CASINO_PALM_ZONE])


def trees(m, mns, mnt, ortho, bmask, osm_pts, stats, no_tree=None):
    palm_zone = casino_palm_zone()
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
        if palm_zone.contains(Point(x, y)):
            continue                    # the square's palms come from the map below
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
    wq_palms(m, mns, mnt, bmask, placed, rnd, stats)
    placed = np.array(placed) if placed else np.zeros((0, 2))
    mntR = Raster(mnt)
    for (x, y), t in osm_pts:
        if t.get("natural") != "tree" or not (X0 < x < X1 and Y0 < y < Y1):
            continue
        if palm_zone.contains(Point(x, y)):
            # A slim palm, 8-12 m, its crown clear of the crowd below.
            z = float(mntR.sample(np.array([x]), np.array([y]))[0])
            if np.isfinite(z):
                add_tree(m, x, y, z, rnd.uniform(8.5, 12.0), rnd.uniform(2.6, 3.2), "palm", rnd)
                stats["trees"] += 1
                stats["casino_palms"] = stats.get("casino_palms", 0) + 1
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
YACHT_PLANS = []          # hull plans as built (for checks)


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


def marina_basins():
    """The harbours' water areas from OpenStreetMap (leisure=marina, as
    closed ways or multipolygon relations: Port Hercule is a relation):
    boats are only looked for inside them, not on the sea face of the
    breakwater or along the open coast."""
    from shapely.ops import linemerge, polygonize
    root = ET.parse(RAW / "osm.xml").getroot()
    tl = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    nodes = {n.get("id"): (float(n.get("lon")), float(n.get("lat"))) for n in root.findall("node")}
    wnodes = {w.get("id"): [nd.get("ref") for nd in w.findall("nd")] for w in root.findall("way")}
    tags = lambda e: {k.get("k"): k.get("v") for k in e.findall("tag")}

    def line(refs):
        ll = np.array([nodes[r] for r in refs if r in nodes])
        if len(ll) < 2:
            return None
        x, y = tl.transform(ll[:, 0], ll[:, 1])
        return LineString(np.column_stack([x, y]))

    out = []
    for w in root.findall("way"):
        r = wnodes[w.get("id")]
        if tags(w).get("leisure") == "marina" and r[0] == r[-1] and (g := line(r)) is not None and len(g.coords) > 3:
            out.append(Polygon(g.coords).buffer(0))
    for rel in root.findall("relation"):
        if tags(rel).get("leisure") != "marina":
            continue
        ls = [line(wnodes[m.get("ref")]) for m in rel.findall("member")
              if m.get("type") == "way" and m.get("role") == "outer" and m.get("ref") in wnodes]
        ls = [g for g in ls if g is not None]
        if ls:
            out += list(polygonize(linemerge(ls)))
    return unary_union(out) if out else None


def mooring_lines(ways):
    """The edges boats tie up to: the quays (OpenStreetMap coastline, which
    follows them round the harbour) and the outlines of piers and
    pontoons."""
    out = []
    bb = box(X0, Y0, X1, Y1)
    for t, p, closed in ways:
        if t.get("natural") != "coastline" and t.get("man_made") not in ("pier", "breakwater", "quay") \
                and t.get("floating") != "yes":
            continue
        g = LineString(p).intersection(bb)
        out += [q for q in getattr(g, "geoms", [g]) if not q.is_empty and q.geom_type == "LineString"]
    return out


def photo_berths(photo, water, ways, moor_d):
    """Where the boats really are, read from the IGN photo (20 cm) along the
    quays and pontoons, as berths: boats in Port Hercule lie stern-to, side
    by side, square to the pontoon. Every 40 cm along each straight stretch
    of quay or pontoon a ray goes out over the water; the white run it
    meets within 4.5 m (a boat's stern) gives that boat's length. Along the
    pontoon the runs make a row: a dip in length between two plateaus is
    the gap between two bows, so each plateau is a boat; a plateau too wide
    for one boat holds several of the same size. A long run no deeper than
    a big yacht's beam, solid along its outer side, is a yacht lying
    alongside. Big white shapes left over (superyachts rafted beside
    another, yachts at anchor) keep their own outline; small ones out in
    the fairway (tenders under way, glints) are dropped.
    Yields (stern point, bow direction, length, beam, how)."""
    ph = np.asarray(photo)
    HR = (X1 - X0) / ph.shape[1]
    rr = np.clip(((np.arange(ph.shape[0]) + 0.5) * HR / RES).astype(int), 0, water.shape[0] - 1)
    cc = np.clip(((np.arange(ph.shape[1]) + 0.5) * HR / RES).astype(int), 0, water.shape[1] - 1)
    wat = water[rr][:, cc]
    lum = ph.astype(np.uint16).sum(-1)
    sat = ph.max(-1).astype(np.int16) - ph.min(-1)
    white = wat & (lum > 315) & (sat < 85)          # brighter than water (~120) and its glints
    # Teak decks: warm brown, never the colour of the water.
    white |= wat & (lum > 230) & (ph[..., 0].astype(np.int16) > ph[..., 2].astype(np.int16) + 25)
    del lum, sat
    k1 = max(1, int(round(0.2 / HR)))
    white = ndimage.binary_opening(white, iterations=k1)
    white = ndimage.binary_closing(white, iterations=2 * k1)
    white = ndimage.binary_fill_holes(white)
    white = ndimage.binary_opening(white, iterations=2 * k1) & wat

    def wsample(x, y):
        c = ((np.asarray(x) - X0) / HR).astype(int)
        r = ((Y1 - np.asarray(y)) / HR).astype(int)
        ok = (c >= 0) & (c < white.shape[1]) & (r >= 0) & (r < white.shape[0])
        o = np.zeros(np.shape(x), bool)
        o[ok] = white[r[ok], c[ok]]
        return o

    waterR = Raster(water.astype(np.float32))
    STEP, DS, MAXD = 0.4, HR, 110.0
    d = np.arange(0, MAXD, DS)
    # A boat's stern is within 4.5 m of the quay (a gangway's length), a
    # superyacht's up to 12 m.
    n_start, n_far, n_gap = int(4.5 / DS), int(12 / DS), int(1.0 / DS)
    rows = []
    for ln in mooring_lines(ways):
        c = np.asarray(ln.simplify(0.8).coords)
        for a, b in zip(c[:-1], c[1:]):
            L = float(np.hypot(*(b - a)))
            if L < 4:
                continue
            t = (b - a) / L
            for nrm in (np.array([-t[1], t[0]]), np.array([t[1], -t[0]])):
                f_ = np.linspace(0.1, 0.9, 9)[:, None, None]
                m_ = (a + (b - a) * f_ + nrm * np.array([[2.0], [4.0], [6.0]])[None]).reshape(-1, 2)
                if np.nanmean(waterR.sample(m_[:, 0], m_[:, 1])) < 0.6:
                    continue
                u = np.arange(STEP / 2, L, STEP)
                w = wsample(a[0] + t[0] * u[:, None] + nrm[0] * d[None], a[1] + t[1] * u[:, None] + nrm[1] * d[None])
                s0 = np.full(len(u), -1.0)
                Lr = np.zeros(len(u))
                for i in np.nonzero(w[:, :n_far].any(1))[0]:
                    row = w[i]
                    k = int(np.argmax(row[:n_far]))
                    # the run, through dark patches (windows, teak) under 1 m
                    off = np.nonzero(~row[k:])[0]
                    j = k
                    for g0 in np.split(off, np.nonzero(np.diff(off) > 1)[0] + 1) if len(off) else []:
                        if len(g0) > n_gap or g0[-1] == len(row) - k - 1:
                            j = k + g0[0] - 1
                            break
                    else:
                        j = len(row) - 1
                    if k < n_start or (j - k + 1) * DS >= 20:
                        s0[i], Lr[i] = k * DS, (j - k + 1) * DS
                rows.append((a, t, nrm, u, s0, Lr))

    boats = []
    for a, t, nrm, u, s0, Lr in rows:
        Lm = ndimage.median_filter(Lr, 3, mode="nearest")
        # Yachts lying alongside first: a long stretch (short breaks
        # allowed: dark decks) no deeper than a big yacht's beam, solid
        # down its middle line, where a row of boats has water between hulls.
        along = np.zeros(len(u), bool)
        lab, nr = ndimage.label(ndimage.binary_closing(Lm >= 3.0, iterations=int(4.0 / STEP)))
        for k in range(1, nr + 1):
            sel = np.nonzero((lab == k) & (Lm >= 3.0))[0]
            if not len(sel):
                continue
            idx = np.arange(sel.min(), sel.max() + 1)
            Lp = Lm[sel]
            if len(Lp) < 4:
                continue
            w = len(idx) * STEP
            Lmed = float(np.percentile(Lp, 75))
            st0 = float(np.median(s0[idx][s0[idx] >= 0]))
            # A hull side is straight: the depth hardly changes along the
            # middle of the yacht, where a row of bows is a saw.
            mid = Lp[len(Lp) // 6: len(Lp) - len(Lp) // 6]
            flat = float(np.mean(np.abs(mid - np.median(mid)) < 0.1 * np.median(mid))) if len(mid) else 0.0
            if 5.5 <= Lmed <= 16 and w >= max(1.8 * Lmed, 18):
                uu = u[idx]
                dd = st0 + 0.5 * Lmed
                cov = wsample(a[0] + t[0] * uu + nrm[0] * dd, a[1] + t[1] * uu + nrm[1] * dd).mean()
                # One solid hull, not a comb of bows: the white in the
                # strip fills most of its convex outline.
                gu, gd = np.meshgrid(uu, np.arange(st0, st0 + Lmed, 0.4))
                gx, gy = a[0] + t[0] * gu + nrm[0] * gd, a[1] + t[1] * gu + nrm[1] * gd
                on = wsample(gx, gy)
                solid = 0.0
                if on.sum() > 10:
                    hull_ = shapely.MultiPoint(np.column_stack([gu[on], gd[on]])).convex_hull
                    solid = on.sum() * 0.4 * STEP / max(hull_.area, 1e-6)
                if (cov > 0.7 and solid > 0.8 and flat > 0.8) or (cov > 0.85 and solid > 0.85 and flat > 0.7):
                    along[idx] = True
                    # The yacht may reach past the end of the quay: follow
                    # it along its middle line both ways.
                    mid_ = a + nrm * (st0 + Lmed / 2)
                    ends = []
                    for sg, u0 in ((-1, uu[0]), (1, uu[-1])):
                        e = np.arange(0, 120, DS)
                        q = u0 + sg * e
                        on = wsample(mid_[0] + t[0] * q, mid_[1] + t[1] * q)
                        gap = np.convolve(~on, np.ones(int(1.5 / DS)), "valid") >= int(1.5 / DS)
                        stop = int(np.argmax(gap)) if gap.any() else len(e) - 1
                        ends.append(u0 + sg * e[stop])
                    Lal = float(min(ends[1] - ends[0], 120))
                    boats.append((mid_ + t * ends[0], t, Lal, Lmed, "alongside", Lal))
        lab, nr = ndimage.label((Lm >= 3.0) & ~along)
        for k in range(1, nr + 1):
            idx = np.nonzero(lab == k)[0]
            w = len(idx) * STEP
            if w < 1.6:
                continue
            Lp = Lm[idx]
            cuts = []
            for i in range(2, len(Lp) - 2):
                if Lp[i] <= Lp[i - 1] and Lp[i] <= Lp[i + 1]:
                    lh = min(Lp[:i].max(), Lp[i + 1:].max())
                    if lh - Lp[i] > max(1.5, 0.2 * lh) and (not cuts or (i - cuts[-1]) * STEP > 1.6):
                        cuts.append(i)
            for pp in np.split(np.arange(len(idx)), cuts):
                if len(pp) * STEP < 1.4:
                    continue
                ii = idx[pp]
                Lb = float(np.percentile(Lm[ii], 80))
                if Lb < 4:
                    continue
                wb = len(ii) * STEP
                nb = max(1, int(round(wb / (np.clip(Lb / 3.2, 2.2, 13) + 0.4))))
                pitch = wb / nb
                # (superyachts are slim: a 60 m yacht is about 11 m wide)
                beam = float(np.clip(pitch - 0.35, Lb / (3.8 if Lb < 20 else 6.5), min(Lb / 2.5, 14)))
                st = float(np.median(s0[ii]))
                st = float(np.clip(st, 0.4, 3.0 if Lb < 20 else 11.0))
                for j in range(nb):
                    uc = u[ii[0]] - STEP / 2 + pitch * (j + 0.5)
                    boats.append((a + t * uc + nrm * st, nrm, Lb, beam, "stern-to", w))
    # Long rows first: where two rows meet at a corner, the row wins.
    boats.sort(key=lambda b: -b[5])
    for b in boats:
        yield b[:5]

    # What the rows did not explain: big yachts rafted outside another one,
    # or at anchor; a mid-size boat right by a quay.
    img = Image.new("L", (white.shape[1], white.shape[0]), 0)
    dr = ImageDraw.Draw(img)
    for st, ax, L, W, _, _ in boats:
        sd = np.array([-ax[1], ax[0]])
        q = [st + ax * uu + sd * vv for uu, vv in hull_outline(L, W)]
        dr.polygon([((x - X0) / HR, (Y1 - y) / HR) for x, y in q], fill=255)
    taken = ndimage.binary_dilation(np.asarray(img) > 0, iterations=int(1.0 / HR))
    lab, n = ndimage.label(white)
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        mk = lab[sl] == i
        if mk.sum() * HR * HR < 12:
            continue
        left = ndimage.binary_opening(mk & ~taken[sl], iterations=int(0.6 / HR))
        if left.sum() < 0.35 * mk.sum() or left.sum() * HR * HR < 12:
            continue
        r_, c_ = np.nonzero(left)
        x = X0 + (c_ + sl[1].start + 0.5) * HR
        y = Y1 - (r_ + sl[0].start + 0.5) * HR
        yield from _outline_boats(np.column_stack([x, y]), HR, moor_d)


def _outline_boats(pts, HR, moor_d, depth=0):
    """One white shape left over by the rows: a yacht in its own outline,
    or several side by side (a raft of superyachts touching), split where
    the shape narrows between hulls."""
    cen = pts.mean(0)
    ev, v = np.linalg.eigh(np.cov((pts - cen).T))
    ax = v[:, 1]
    pr, pe = (pts - cen) @ ax, (pts - cen) @ v[:, 0]
    L, W = pr.max() - pr.min() + HR, pe.max() - pe.min() + HR
    if L >= 20 and W > L / 3.2 and depth < 3:
        bins = np.arange(pe.min(), pe.max() + 0.4, 0.4)
        h, _ = np.histogram(pe, bins)
        h = ndimage.uniform_filter1d(h.astype(float), 3)
        lo = int(np.argmin(h[len(h) // 6: len(h) - len(h) // 6])) + len(h) // 6 if len(h) > 12 else -1
        if lo > 0 and h[lo] < 0.6 * min(h[:lo].max(), h[lo + 1:].max()):
            cut = bins[lo]
            for part in (pe < cut, pe >= cut):
                if part.sum() * HR * HR > 12:
                    yield from _outline_boats(pts[part], HR, moor_d, depth + 1)
            return
    fill = len(pts) * HR * HR / max(L * W, 1)
    if fill < 0.5 or L / W > 6 or L > 140 or W < 1.8:
        return
    mid = cen + ax * (pr.max() + pr.min()) / 2
    dl = lambda p: float(moor_d[int(np.clip((Y1 - p[1]) / RES, 0, moor_d.shape[0] - 1)),
                                int(np.clip((p[0] - X0) / RES, 0, moor_d.shape[1] - 1))])
    near = min(dl(mid + ax * L / 2), dl(mid - ax * L / 2), dl(mid))
    if L < 14 and (L < 7 or near > 6):
        return                             # under way in the fairway, or a glint
    if near > 12 and FONTVIEILLE_PORT.contains(Point(*mid)):
        return                             # Port de Fontvieille: wakes stirred white, mid-basin
    if dl(mid + ax * L / 2) < dl(mid - ax * L / 2):
        ax = -ax                           # bow away from the quay
    yield mid - ax * L / 2, ax, L, float(min(W, L / 2.8)), "photo outline"


def mareterra_berths(ways, sea, on_pier):
    """Port du Petit Portier, Mareterra's marina (opened 2024, after the
    photo): about 16 berths for boats up to 16 m, stern-to along its two
    long quays, the fairway down the middle kept clear, nothing across the
    head of the basin or in its mouth. Yields like photo_berths."""
    mm = None
    for t, pts_, closed in ways:
        if closed and t.get("leisure") == "marina" and len(pts_) > 3:
            g = Polygon(pts_).buffer(0)
            if MT["zone"].buffer(30).contains(g.representative_point()):
                mm = g
    if mm is None:
        return
    seaR = Raster((sea & ~on_pier).astype(np.float32))
    wet = lambda p: seaR.sample(np.array([p[0]]), np.array([p[1]]))[0] > 0.5
    rb = np.random.default_rng(16)
    ring = np.asarray(orient(mm, 1.0).exterior.coords)
    # The basin's long sides: the two longest edges of its outline that
    # face each other (roughly parallel).
    edges = []
    for a, b in zip(ring[:-1], ring[1:]):
        L = float(np.hypot(*(b - a)))
        if L > 25:
            edges.append((L, a, b))
    edges.sort(key=lambda e: -e[0])
    if not edges:
        return
    t0 = (edges[0][2] - edges[0][1]) / edges[0][0]
    facing = [e for e in edges[1:] if abs(float((e[2] - e[1]) @ t0)) / e[0] > 0.8]
    edges = edges[:1] + facing[:1]
    tot = sum(e[0] - 14 for e in edges)
    for L, a, b in edges:
        used, cap = 0, int(round(16 * (L - 14) / tot))
        t = (b - a) / L
        n = np.array([-t[1], t[0]])        # inward for a counter-clockwise ring
        if not wet((a + b) / 2 + n * 4):
            n = -n
        # Keep clear of the corners (the quay's ends, the mouth).
        s = 7.0
        while s < L - 7.0 and used < cap:
            Lb = float(rb.choice([9.5, 11, 12, 13, 14, 15, 16]))
            beam = round(Lb / 3.1, 1)
            p = a + t * (s + beam / 2) + n * 0.8
            if all(wet(p + n * q) for q in (1.0, Lb * 0.5, Lb + 0.5)):
                if rb.random() > 0.12:     # one or two berths free
                    yield p, n, Lb, beam, "marina"
                    used += 1
            s += beam + 0.9


def yachts(m, ortho, sea, stats, ways=(), photo=None):
    """The boats in Port Hercule, where the IGN photo shows them (see
    photo_berths), and in Mareterra's marina (see mareterra_berths), rebuilt
    as clean stylised boats: white hull with a dark boot stripe, decks and
    stepped superstructures with dark window bands; small open boats and
    a few sailing yachts among them."""
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
    moor = ~sea | on_pier
    moor_d = ndimage.distance_transform_edt(~moor) * RES
    water = sea & ~on_pier
    if MT is not None:
        water &= ~MT["mask"]          # Mareterra: the 2022 photo shows work barges
    stats["boats_by_berth"] = {}

    placed = []

    def build(stern, ax, L, W, sail_odds=0.1):
        side = np.array([-ax[1], ax[0]])
        # What sort of boat: under 8.5 m an open day boat; a share of the
        # 9-18 m berths are sailing yachts (slimmer, a mast); the rest
        # motor yachts.
        sail = 9 <= L <= 18 and rnd.random() < sail_odds
        if sail:
            W = min(W, L / 3.3)

        def plan(along0, along1, half):
            q = [(stern + ax * a + side * s) for a, s in
                 [(along0, -half), (along1, -half), (along1, half), (along0, half)]]
            return orient(Polygon(q), 1.0)

        hull = orient(Polygon([stern + ax * u + side * s for u, s in hull_outline(L, W)]), 1.0)
        if not hull.is_valid:
            hull = hull.buffer(0)
        if not isinstance(hull, Polygon):
            return False
        # Two rows meeting at a quay corner: no boat inside another.
        for h_ in placed:
            if h_.intersects(hull) and h_.intersection(hull).area > 0.08 * min(h_.area, hull.area):
                return False
        placed.append(hull)
        YACHT_PLANS.append(hull)
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
        if L < 8.5:
            # Open day boat: a low cockpit, a centre console with its
            # windscreen, a sun cushion on the foredeck.
            stats["yachts"] += 1
            stats["day_boats"] = stats.get("day_boats", 0) + 1
            for q in to_polys(plan(0.08 * L, 0.6 * L, W * 0.36).intersection(hull.buffer(-0.25))):
                add_roof(m, orient(q, 1.0), hull_h * 1.06 + 0.03, hexrgb("#d8d3c7"), False)
            c_ = to_xy(np.array([[0.5 * L, 0.0]]))[0]
            _box(m, *c_, ax, 0.9, 0.75, hull_h, hull_h + 0.75, white * 0.97)
            g_ = to_xy(np.array([[0.5 * L + 0.5, 0.0]]))[0]
            _box(m, *g_, ax, 0.08, 0.9, hull_h + 0.75, hull_h + 1.15, glass)
            for q in to_polys(plan(0.66 * L, 0.84 * L, W * 0.3).intersection(hull.buffer(-0.2))):
                add_roof(m, orient(q, 1.0), hull_h + 0.12, hexrgb(rnd.choice(["#e9e4d6", "#2f4a68", "#d8d2c0"])), False)
            return True
        if sail:
            # Sailing yacht: a low coachroof with dark ports, a cockpit
            # aft, the mast a third of the way back and the boom to the
            # cockpit, sail furled under a blue cover.
            stats["yachts"] += 1
            stats["sailing_yachts"] = stats.get("sailing_yachts", 0) + 1
            lo = to_xy(np.array(hull_outline(0.42 * L, W * 0.62)) + [0.3 * L, 0])
            hi = to_xy(np.array(hull_outline(0.4 * L, W * 0.55)) + [0.31 * L, 0])
            loft(m, lo, np.full(len(lo), hull_h), hi, np.full(len(hi), hull_h + 0.7), top, HULL, band=True)
            roof = Polygon(hi).buffer(0)
            if isinstance(roof, Polygon):
                add_roof(m, orient(roof, 1.0), hull_h + 0.7, top * 0.97, False)
            for q in to_polys(plan(0.05 * L, 0.28 * L, W * 0.33).intersection(hull.buffer(-0.3))):
                add_roof(m, orient(q, 1.0), hull_h * 1.06 + 0.03, hexrgb("#b88a5c"), False)
            mp = to_xy(np.array([[0.62 * L, 0.0]]))[0]
            mh = 1.35 * L + 1.5
            steel = np.array(hexrgb("#c9ccd0"))
            _box(m, *mp, ax, 0.22, 0.16, hull_h + 0.7, hull_h + mh, steel, style=METAL)
            bm_ = to_xy(np.array([[0.62 * L - 0.18 * L, 0.0]]))[0]
            _box(m, *bm_, ax, 0.36 * L, 0.42, hull_h + 1.6, hull_h + 2.05, np.array(hexrgb("#1f3f66")))
            return True
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
        return True

    found = photo_berths(photo if photo is not None else ortho, water, ways, moor_d)
    basins = marina_basins()
    if basins is not None:
        bz = shapely.prepared.prep(basins.buffer(4.0))
        found = (b for b in found if b[4] == "marina" or bz.contains(Point(*(b[0] + b[1] * b[2] / 2))))
    if MT is not None:
        found = itertools.chain(found, mareterra_berths(ways, sea, on_pier))
    for stern, ax, L, W, how in found:
        if build(stern, ax, L, W, sail_odds=0.3 if how == "marina" else 0.1):
            stats["boats_by_berth"][how] = stats["boats_by_berth"].get(how, 0) + 1


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


# Hôtel Métropole Monte-Carlo (4 avenue de la Madone), above Casino square
# on the way down to Mirabeau. Neither BD TOPO nor OpenStreetMap has its
# garden as a building, and the OSM hotel point (METROPOLE) lies in that
# garden, outside every outline, so the hotel never got its landmark look:
# it came out as two random orange blocks behind a glass box, with a bare
# lawn and the pool in the gap ("a void with water"). What stands there
# (Mapillary 2018 and 2025 street photos, IGN LiDAR 2021, IGN 20 cm photo):
#   - along the circuit, the Métropole shopping gallery (BD TOPO 070307 and
#     the front of 070330): two storeys of shops in cream stone, then the
#     glazed gallery with its arched, dark green iron windows;
#   - above it, set back, the hotel (070330, 070415): cream Belle Époque
#     walls with white balustraded balconies, a curved south-east corner
#     with a small lead-grey cupola, ~34 m above the circuit;
#   - on the Avenue de la Madone side, the hotel's garden: the Odyssey pool
#     on a pale stone deck at street level (44 m NGF) with loungers and
#     white parasols, and a lower terrace (39.5 m) with a row of white
#     pavilions with arched openings along the gallery's back.
METROPOLE = (1056574.0, 6303546.0)
METROPOLE_HOTEL = ("BATIMENT0000000074070330", "BATIMENT0000000074070415")
METROPOLE_GALLERY = "BATIMENT0000000074070307"
METROPOLE_CUPOLA = (50.0, 33.5)      # m from METROPOLE: inside the curved corner
METROPOLE_DECK_AXIS = 33.0           # degrees east of north: Avenue de la Madone
METROPOLE_DECK = ((-36.5, -18.0), 34.0, 19.0)    # centre, length along the avenue, width
METROPOLE_PAVILIONS = ((-17.5, -46.0), (6.0, -4.0))   # lower terrace, south and north ends
METROPOLE_STONE = "#eee2c8"


def _balustrade(m, a, b, z, stone, top_col, plinth=0.6, height=1.45):
    """A stone balustrade from a to b standing on the ground at z: a plinth,
    a row of balusters and a coping rail."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    d = b - a
    L = float(np.linalg.norm(d))
    if L < 0.3:
        return
    u = d / L
    base = orient(LineString([a, b]).buffer(0.22, cap_style=2, join_style=2), 1.0)
    add_walls(m, base, z - 0.4, z + plinth, z, 100, stone, PLAIN)
    add_roof(m, base, z + plinth, stone, False)
    nb = max(1, int(L / 0.32))
    for k in range(nb):
        p = a + d * (k + 0.5) / nb
        _box(m, *p, u, 0.13, 0.13, z + plinth, z + height - 0.16, stone * 1.03)
    cap = orient(LineString([a, b]).buffer(0.2, cap_style=2, join_style=2), 1.0)
    add_walls(m, cap, z + height - 0.16, z + height, z, 100, top_col, PLAIN)
    add_roof(m, cap, z + height, top_col, False)


def metropole(m, ways, mntR, mns, stats):
    """The hotel's garden and its corner cupola (the walls themselves come
    from buildings(), with the landmark colour and the survey's balconies)."""
    ox, oy = METROPOLE
    rel = lambda x, y: (ox + x, oy + y)
    stone = np.array(hexrgb(METROPOLE_STONE))
    deckc = np.array(hexrgb("#e4dccb"))
    white = np.array(hexrgb("#f6f3ec"))
    water = np.array(hexrgb("#4fb6c6"))
    zinc = np.array(hexrgb("#8b9497"))
    n = {"parasols": 0, "loungers": 0, "pavilions": 0}

    # Pool deck: a stone terrace at street level, kerb walls round it.
    a = math.radians(METROPOLE_DECK_AXIS)
    u = np.array([math.sin(a), math.cos(a)])
    v = np.array([u[1], -u[0]])                     # across, towards the gallery
    (cx, cy), L, W = METROPOLE_DECK
    c = np.array(rel(cx, cy))
    deck = orient(Polygon([c - u * L / 2 - v * W / 2, c + u * L / 2 - v * W / 2,
                           c + u * L / 2 + v * W / 2, c - u * L / 2 + v * W / 2]), 1.0)
    zs = mntR.sample(np.array([c[0]]), np.array([c[1]]))
    zd = float(zs[0]) + 0.15 if np.isfinite(zs).all() else 44.0
    pools = [Polygon(p_).buffer(0) for tg, p_, cl in ways if cl and len(p_) > 3
             and tg.get("leisure") == "swimming_pool"]
    pools = [p for p in pools if deck.buffer(2).contains(p.centroid)]
    holes = [orient(p.buffer(0.6, join_style=2), 1.0) for p in pools]
    top = deck.difference(unary_union(holes)) if holes else deck
    for p in to_polys(top):
        add_roof(m, orient(p, 1.0), zd, deckc, False)
    add_walls(m, deck, zd - 6.0, zd, zd, 100, stone * 0.94, PLAIN)
    # A balustrade on the side over the lower terrace.
    b0, b1 = c + v * W / 2 - u * L / 2, c + v * W / 2 + u * L / 2
    for k in range(int(L / 0.45)):
        p = b0 + (b1 - b0) * (k + 0.5) / int(L / 0.45)
        _box(m, *(p - v * 0.15), u, 0.14, 0.14, zd, zd + 0.85, white)
    rail = orient(LineString([b0 - v * 0.15, b1 - v * 0.15]).buffer(0.16, cap_style=2, join_style=2), 1.0)
    add_walls(m, rail, zd + 0.85, zd + 1.0, zd, 100, white, PLAIN)
    add_roof(m, rail, zd + 1.0, white, False)
    # The garden is closed off from the Avenue de la Madone (2018 street
    # photos): a cream stone balustrade on a plinth along the pavement and,
    # right behind it, a clipped hedge taller than a person (with the trees
    # the LiDAR sees over it), so from the street the pool is out of sight.
    # Same hedge across the deck's south-west end.
    leaf = np.array(hexrgb("#3d5a2c"))
    m0, m1 = c - v * W / 2 - u * L / 2, c - v * W / 2 + u * L / 2
    s0 = c + v * W / 2 - u * L / 2
    nseg = max(1, int(L / 4.0))
    for k in range(nseg):
        a_, b_ = m0 + (m1 - m0) * k / nseg, m0 + (m1 - m0) * (k + 1) / nseg
        mid = (a_ + b_) / 2 - v * 2.0
        zz = mntR.sample(np.array([mid[0]]), np.array([mid[1]]))
        zst = float(zz[0]) if np.isfinite(zz).all() else zd
        _balustrade(m, a_ - v * 0.35, b_ - v * 0.35, min(zst, zd), stone, white)
        hg = orient(LineString([a_ + v * 0.7, b_ + v * 0.7]).buffer(0.75, cap_style=2, join_style=2), 1.0)
        add_walls(m, hg, zd - 0.2, zst + 4.6, zd, 100, leaf, LEAF)
        add_roof(m, hg, zst + 4.6, leaf * 1.08, False)
    hg = orient(LineString([m0 + u * 0.7 + v * 1.5, s0 + u * 0.7 - v * 0.6]).buffer(0.75, cap_style=2, join_style=2), 1.0)
    add_walls(m, hg, zd - 0.2, zd + 3.2, zd, 100, leaf, LEAF)
    add_roof(m, hg, zd + 3.2, leaf * 1.08, False)
    for p in pools:
        # The Odyssey pool: water a little below a white stone coping.
        cop = orient(p.buffer(0.6, join_style=2), 1.0)
        for q in to_polys(cop.difference(p)):
            add_roof(m, orient(q, 1.0), zd + 0.08, white, False)
        add_walls(m, orient(p, 1.0), zd - 0.25, zd + 0.08, zd, 100, water * 0.85, PLAIN, inward=True)
        add_roof(m, orient(p, 1.0), zd - 0.25, water, False)
        # Loungers and white parasols round it, on the deck.
        pc = p.centroid
        rr = p.exterior
        for t in np.linspace(0, 1, 14, endpoint=False):
            e = rr.interpolate(t, normalized=True)
            d = np.array([e.x - pc.x, e.y - pc.y])
            d /= np.linalg.norm(d) + 1e-9
            q = np.array([e.x, e.y]) + d * 2.0
            if not deck.buffer(-0.8).contains(shapely.Point(*q)):
                continue
            _box(m, *q, d, 1.9, 0.7, zd, zd + 0.35, white)
            n["loungers"] += 1
            if int(t * 14) % 2 == 0:
                s = q + d * 1.6
                _box(m, *s, d, 0.07, 0.07, zd, zd + 2.3, zinc)
                ring = [s + np.array([math.cos(t_), math.sin(t_)]) * 1.4 for t_ in np.linspace(0, 2 * math.pi, 9)[:-1]]
                P = enu_fast(np.array([r[0] for r in ring] + [s[0]]), np.array([r[1] for r in ring] + [s[1]]),
                             np.array([zd + 2.05] * 8 + [zd + 2.6]))
                for i in range(8):
                    m.tri(P[[8]], P[[i]], P[[(i + 1) % 8]], white, PLAIN)
                n["parasols"] += 1

    # Lower terrace: white pavilions with arched openings along the back
    # of the gallery, flat roofs at the deck's level.
    p0, p1 = np.array(rel(*METROPOLE_PAVILIONS[0])), np.array(rel(*METROPOLE_PAVILIONS[1]))
    dl = np.linalg.norm(p1 - p0)
    du = (p1 - p0) / dl
    dv = np.array([du[1], -du[0]])
    k_ = 5
    gap = 1.6
    seg = (dl - gap * (k_ - 1)) / k_
    for i in range(k_):
        q0 = p0 + du * i * (seg + gap)
        q1 = q0 + du * seg
        pv = orient(Polygon([q0 - dv * 2.2, q1 - dv * 2.2, q1 + dv * 2.2, q0 + dv * 2.2]), 1.0)
        zz = mntR.sample(np.array([(q0[0] + q1[0]) / 2]), np.array([(q0[1] + q1[1]) / 2]))
        z0 = float(zz[0]) if np.isfinite(zz).all() else 39.5
        h = min(max(zd - z0 + 0.3, 3.6), 6.0)
        add_walls(m, pv, z0 - 0.5, z0 + h, z0, h, stone, WALL_OLD, style=S_ARCH)
        add_roof(m, pv, z0 + h, np.array(hexrgb("#d8cfbe")), False)
        cor = orient(pv.buffer(0.3, join_style=2), 1.0)
        add_walls(m, cor, z0 + h - 0.35, z0 + h, z0 + h, 100, white, PLAIN)
        for rp in to_polys(cor.difference(pv)):
            add_roof(m, orient(rp, 1.0), z0 + h, white, False)
        n["pavilions"] += 1

    # The cupola on the curved corner: a drum ringed with oculi, a lead
    # dome and a small lantern, on the hotel's roof.
    qx, qy = rel(*METROPOLE_CUPOLA)
    r_, c_ = int((Y1 - qy) / RES), int((qx - X0) / RES)
    win = mns[max(r_ - 8, 0):r_ + 9, max(c_ - 8, 0):c_ + 9]
    zr = float(np.nanpercentile(win, 50)) if np.isfinite(win).any() else 64.0
    drum = orient(Point(qx, qy).buffer(2.6, 12), 1.0)
    add_walls(m, drum, zr - 0.5, zr + 3.0, zr - 0.5, 3.5, stone, WALL_OLD, style=S_ARCH)
    cor = orient(Point(qx, qy).buffer(2.95, 12), 1.0)
    add_walls(m, cor, zr + 2.7, zr + 3.1, zr + 3.1, 100, white, PLAIN)
    add_roof(m, cor, zr + 3.1, white, False)
    _ball(m, qx, qy, zr + 3.1, 2.45, zinc, None, 14, 8)
    _box(m, qx, qy, (1, 0), 0.9, 0.9, zr + 5.4, zr + 6.6, white)
    _ball(m, qx, qy, zr + 6.7, 0.5, zinc, None, 8, 5)
    stats["metropole"] = n
    return n


# PLAIN styles drawn by the viewer: 5 polished steel, 6 bronze.
METAL, BRONZE = 5, 6


# --------------------------------------------------------- Palais Héraclès ----
# 17 Boulevard Albert 1er: the 1950s residential slab that stands over the
# start straight, facing Port Hercule. BD TOPO's outline for it is the low
# block it shares with its neighbours (5 m), so the generic builder made it
# a LiDAR massing with a random 1970s-tower façade. Rebuilt here from the
# LiDAR (the slab: 18 x 35.5 m, flat roof at 59.4 m NGF, a lift room at the
# north end) and Mapillary street photos (2019 and 2025, from the boulevard
# looking north): smooth cream render; on the long sides a regular grid of
# recessed loggias behind solid parapets, many with blue roller awnings; on
# the south end a single stack of projecting box balconies on the west
# half, the rest blank; the north end blank but for one column of windows;
# a set-back top floor under a thin flat roof that overhangs the long
# sides, a square post at the south-west corner. 16 levels (OpenStreetMap):
# a tall ground floor, 14 storeys of flats and the top floor. Nobody has
# photographed the west side from the street (it faces the courtyards off
# Rue Louis Notari); it gets the same loggias as the east side.
HERACLES = "BATIMENT0000000074074389"
HERACLES_SW = (1056039.35, 6302815.95)  # the slab's corners, from the LiDAR (Lambert-93)
HERACLES_SE = (1056056.55, 6302818.95)
HERACLES_D = 35.3                       # m, south to north
HERACLES_LIFT = (1056042.3, 6302842.9)  # lift room on the roof (LiDAR, up to 62.8 m)
HERACLES_WALL = "#efe1bf"               # cream render
HERACLES_CANOPY_TIP = (1056067.4, 6302842.9)   # entrance canopy, its tip over the pavement
HERACLES_AWNING = "#2d4f93"             # blue awnings


def palais_heracles(m, mntR, stats):
    SW, SE = np.array(HERACLES_SW), np.array(HERACLES_SE)
    W = float(np.linalg.norm(SE - SW))
    u = (SE - SW) / W                    # along the south end, west to east
    v = np.array([-u[1], u[0]])          # along the long sides, south to north
    D = HERACLES_D
    gz = mntR.sample(np.array([SW[0], SE[0], (SW + D * v)[0], (SE + D * v)[0]]),
                     np.array([SW[1], SE[1], (SW + D * v)[1], (SE + D * v)[1]]))
    base = float(np.nanmin(gz)) if np.isfinite(gz).any() else 4.0
    wall = np.array(hexrgb(HERACLES_WALL))
    edge = np.minimum(wall * 1.04 + 6, 255)          # slab edges and parapets catch more light
    inner = wall * 0.86                              # loggia walls, in the shade
    shutter = np.array(hexrgb("#e9e6de"))
    awning = np.array(hexrgb(HERACLES_AWNING))
    glass_c = np.array(hexrgb("#5b6a74"))
    roof_c = np.array(hexrgb("#d3ccbd"))
    rnd = rng_for(HERACLES)

    def E(s, t, z):
        s, t, z = np.broadcast_arrays(np.atleast_1d(np.asarray(s, float)), np.atleast_1d(np.asarray(t, float)),
                                      np.atleast_1d(np.asarray(z, float)))
        q = SW[None] + s[:, None] * u[None] + t[:, None] * v[None]
        return enu_fast(q[:, 0], q[:, 1], z)

    def want(s, t, z, ds, dt, dz):
        return E(s + ds, t + dt, z + dz) - E(s, t, z)

    def cuboid(s0, s1, t0, t1, z0, z1, col, bottom=True, top=True):
        """A box in the slab's frame (s across, t along, z up): all its faces."""
        sc, tc, zc = (s0 + s1) / 2, (t0 + t1) / 2, (z0 + z1) / 2
        faces = [((s0, t0, z0), (s0, t1, z0), (s0, t1, z1), (s0, t0, z1), (-1, 0, 0)),
                 ((s1, t0, z0), (s1, t1, z0), (s1, t1, z1), (s1, t0, z1), (1, 0, 0)),
                 ((s0, t0, z0), (s1, t0, z0), (s1, t0, z1), (s0, t0, z1), (0, -1, 0)),
                 ((s0, t1, z0), (s1, t1, z0), (s1, t1, z1), (s0, t1, z1), (0, 1, 0))]
        if top:
            faces.append(((s0, t0, z1), (s1, t0, z1), (s1, t1, z1), (s0, t1, z1), (0, 0, 1)))
        if bottom:
            faces.append(((s0, t0, z0), (s1, t0, z0), (s1, t1, z0), (s0, t1, z0), (0, 0, -1)))
        for a, b, c, d, n in faces:
            _quad(m, E(*a), E(*b), E(*c), E(*d), col, want(sc, tc, zc, *n))

    def pane(s0, t0, s1, t1, z0, z1, n, cell, floor_):
        """Window glass on a wall (from (s0, t0) to (s1, t1)), facing n: the
        viewer's glass façade style, so it reflects by day and some
        windows light up at night."""
        uvs = [np.array([[cell + 0.06, floor_ + 0.18]]), np.array([[cell + 0.94, floor_ + 0.18]]),
               np.array([[cell + 0.94, floor_ + 0.9]]), np.array([[cell + 0.06, floor_ + 0.9]])]
        _quad_uv(m, E(s0, t0, z0), E(s1, t1, z0), E(s1, t1, z1), E(s0, t0, z1), glass_c,
                 want((s0 + s1) / 2, (t0 + t1) / 2, z0, *n), uvs, kind=WALL_GLASS, style=S_GLASS)

    def awning_(s_front, t0, t1, z, out):
        """A blue roller awning hung under a soffit: from the loggia's front
        edge, sloping out and down (out = +1 east, -1 west)."""
        a0, a1 = E(s_front, t0, z), E(s_front, t1, z)
        b0, b1 = E(s_front + out * 0.75, t0, z - 0.85), E(s_front + out * 0.75, t1, z - 0.85)
        sc_ = s_front + out * 0.4
        for sg in (1, -1):
            _quad(m, a0, a1, b1, b0, awning, want(sc_, (t0 + t1) / 2, z - 0.4, sg * out * 0.75, 0, sg * 0.75))

    GF = 4.5                             # ground floor height
    FH = 3.4                             # storey height
    NF = 14                              # storeys of flats
    zf = lambda i: base + GF + (i - 1) * FH          # floor level of storey i (1..NF)
    ztop = base + GF + NF * FH                       # top of the slab's walls (~56.1 m)
    zr = ztop + 3.05                                 # the top floor's roof (LiDAR: 59.4 m)
    DL = 1.35                                        # loggia depth
    CORNER = 2.4                                     # solid wall at each end of the long sides

    # Ground floor: a tall plinth, glazed onto the boulevard (east).
    cuboid(0.35, W - 0.35, 0.0, D, base - 1.0, base + GF, wall, bottom=False)
    for side, sf in ((1, W - 0.35 + 0.03), (-1, 0.35 - 0.03)):
        n_g = int(round((D - 2 * CORNER) / 2.4))
        for k in range(n_g):
            t0 = CORNER + k * (D - 2 * CORNER) / n_g
            pane(sf, t0 + 0.12, sf, t0 + (D - 2 * CORNER) / n_g - 0.12, base + 0.25, base + GF - 0.55, (side, 0, 0), k, 1)
    # Core of the storeys, behind the loggias, and the solid ends of the long sides.
    cuboid(DL, W - DL, 0.0, D, base + GF, ztop, wall, bottom=False)
    for s0, s1 in ((0.0, DL), (W - DL, W)):
        cuboid(s0, s1, 0.0, CORNER, base + GF - 0.25, ztop, wall)
        cuboid(s0, s1, D - CORNER, D, base + GF - 0.25, ztop, wall)

    # The long sides: a grid of loggias, piers at each bay.
    span = D - 2 * CORNER
    nb = int(round(span / 3.45))
    bay = span / nb
    for side in (1, -1):
        front = W if side > 0 else 0.0
        back = W - DL if side > 0 else DL
        sin_ = lambda a, b: (min(a, b), max(a, b))
        for k in range(nb + 1):
            t = CORNER + k * bay
            if 0 < k < nb:
                s0, s1 = sin_(front - side * 0.02, back)
                cuboid(s0, s1, t - 0.22, t + 0.22, base + GF - 0.25, ztop, wall)
        for i in range(1, NF + 1):
            z = zf(i)
            # Floor slab (its edge 2 cm behind the piers) and a solid parapet.
            s0, s1 = sin_(front - side * 0.03, back)
            cuboid(s0, s1, CORNER, D - CORNER, z - 0.22, z, edge)
            s0, s1 = sin_(front - side * 0.05, front - side * 0.2)
            cuboid(s0, s1, CORNER, D - CORNER, z, z + 1.0, edge)
            for k in range(nb):
                t0, t1 = CORNER + k * bay + 0.25, CORNER + (k + 1) * bay - 0.25
                sb = back + side * 0.03
                if rnd.random() < 0.14:
                    # A window with its pale roller shutter down.
                    _quad(m, E(sb, t0 + 0.12, z + 0.1), E(sb, t1 - 0.12, z + 0.1), E(sb, t1 - 0.12, z + 2.6),
                          E(sb, t0 + 0.12, z + 2.6), shutter, want(sb, (t0 + t1) / 2, z + 1, side, 0, 0))
                else:
                    pane(sb, t0 + 0.12, sb, t1 - 0.12, z + 0.1, z + 2.6, (side, 0, 0), k, i)
                if rnd.random() < 0.55:
                    awning_(front - side * 0.1, t0 + 0.02, t1 - 0.02, z + FH - 0.3, side)
                    stats["heracles_awnings"] = stats.get("heracles_awnings", 0) + 1
        # The top floor's glazing, set back behind a terrace.
        sa = W - DL if side > 0 else DL
        n_top = int(round((D - 0.8) / 3.5))
        for k in range(n_top):
            t0 = 0.4 + k * (D - 0.8) / n_top
            t1 = t0 + (D - 0.8) / n_top - 0.3
            pane(sa + side * 0.03, t0, sa + side * 0.03, t1, ztop + 0.1, zr - 0.35, (side, 0, 0), k, NF + 1)
        railing(m, np.array([[*(SW + (front - side * 0.12) * u + 0.15 * v)]]),
                np.array([[*(SW + (front - side * 0.12) * u + (D - 0.15) * v)]]), ztop, "#dfe3e1", h=1.05)

    # The south end: one stack of box balconies on its west half.
    sa_, sb_ = 2.9, 9.3
    for i in range(1, NF + 2):
        z = zf(i) if i <= NF else ztop
        h_ = FH if i <= NF else zr - ztop
        cuboid(sa_, sb_, -1.35, 0.0, z - 0.2, z, edge)                       # slab
        cuboid(sa_ + 0.18, sb_ - 0.18, -1.35, -1.2, z, z + 1.05, edge)      # parapet
        for s0 in (sa_, sb_ - 0.18):
            cuboid(s0, s0 + 0.18, -1.35, 0.0, z, z + h_, wall, bottom=False)  # cheeks
        pane(sa_ + 0.6, -0.03, sb_ - 0.6, -0.03, z + 0.1, z + 2.6, (0, -1, 0), 0, i)
        if rnd.random() < 0.5:
            # Awnings across the balcony's mouth.
            a0, a1 = E(sa_ + 0.2, -1.3, z + h_ - 0.25), E(sb_ - 0.2, -1.3, z + h_ - 0.25)
            b0, b1 = E(sa_ + 0.2, -1.95, z + h_ - 1.05), E(sb_ - 0.2, -1.95, z + h_ - 1.05)
            for sg in (1, -1):
                _quad(m, a0, a1, b1, b0, awning, want((sa_ + sb_) / 2, -1.6, z + h_ - 0.6, 0, -0.6 * sg, 0.7 * sg))
    # The north end: one column of windows.
    for i in range(1, NF + 1):
        pane(W * 0.62 + 0.8, D + 0.03, W * 0.62 - 0.8, D + 0.03, zf(i) + 0.9, zf(i) + 2.4, (0, 1, 0), 0, i)

    # Cornice band round the slab's top, the top floor and its roof.
    cuboid(-0.12, W + 0.12, -0.12, D + 0.12, ztop - 0.32, ztop, edge)
    cuboid(DL, W - DL, 0.0, D, ztop, zr, wall, bottom=False)
    for t0, t1 in ((0.0, 0.3), (D - 0.3, D)):
        # The ends rise to the roof, so the south face reads as one wall.
        for s0, s1 in ((0.0, DL), (W - DL, W)):
            cuboid(s0, s1, t0, t1, ztop, zr, wall, bottom=False)
    cuboid(-0.85, W + 0.85, -0.05, D + 0.05, zr, zr + 0.32, edge)                # thin overhanging roof
    _quad(m, E(-0.85, -0.05, zr + 0.33), E(W + 0.85, -0.05, zr + 0.33), E(W + 0.85, D + 0.05, zr + 0.33),
          E(-0.85, D + 0.05, zr + 0.33), roof_c, want(W / 2, D / 2, zr, 0, 0, 1))
    # Square post at the south-west corner, with a cap.
    cuboid(-0.25, 1.05, -0.25, 1.05, zr + 0.32, zr + 1.25, wall)
    cuboid(-0.4, 1.2, -0.4, 1.2, zr + 1.25, zr + 1.45, edge)
    # Lift room (LiDAR).
    lc = np.array(HERACLES_LIFT) - SW
    ls_, lt_ = float(lc @ u), float(lc @ v)
    cuboid(ls_ - 4.6, ls_ + 4.6, lt_ - 2.5, lt_ + 2.5, zr + 0.32, base + 58.6, wall * 0.97, bottom=False)
    cuboid(ls_ - 4.75, ls_ + 4.75, lt_ - 2.65, lt_ + 2.65, base + 58.6, base + 58.85, edge)
    # The entrance, mid-way along the boulevard side: a folded concrete
    # canopy cantilevered from the slab out to the pavement (OpenStreetMap
    # way 1081601378), thick at the root and thin at the tip, between low
    # planters with clipped hedges.
    tip = np.array(HERACLES_CANOPY_TIP) - SW
    s_tip, t_c = float(tip @ u), float(tip @ v)
    conc = np.array(hexrgb("#bdb8ae"))
    hw_ = 2.1
    zt_, zr_b, zt_b = base + 4.25, base + 2.35, base + 3.8
    cs = [W - DL, s_tip]
    for sg in (-1, 1):
        # Side faces: trapezoids.
        P0, P1 = E(cs, t_c + sg * hw_, [zr_b, zt_b]), E(cs, t_c + sg * hw_, zt_)
        _quad(m, P0[[0]], P0[[1]], P1[[1]], P1[[0]], conc, want(W + 3, t_c + sg * hw_, zt_ - 0.5, 0, sg, 0))
    _quad(m, E(cs[0], t_c - hw_, zt_), E(cs[1], t_c - hw_, zt_), E(cs[1], t_c + hw_, zt_), E(cs[0], t_c + hw_, zt_),
          conc * 1.04, want(W + 3, t_c, zt_, 0, 0, 1))
    _quad(m, E(cs[0], t_c - hw_, zr_b), E(cs[1], t_c - hw_, zt_b), E(cs[1], t_c + hw_, zt_b), E(cs[0], t_c + hw_, zr_b),
          conc * 0.82, want(W + 3, t_c, zr_b, 0, 0, -1))
    _quad(m, E(cs[1], t_c - hw_, zt_b), E(cs[1], t_c + hw_, zt_b), E(cs[1], t_c + hw_, zt_), E(cs[1], t_c - hw_, zt_),
          conc, want(s_tip, t_c, zt_, 1, 0, 0))
    # Glass doors under it.
    pane(W - 0.35 + 0.05, t_c - 1.6, W - 0.35 + 0.05, t_c + 1.6, base + 0.05, base + 2.6, (1, 0, 0), 3, 1)
    stone_ = np.array(hexrgb("#d6ccb8"))
    hedge = np.array(hexrgb("#4e6a3c"))
    for t0, t1 in ((t_c + hw_ + 0.6, t_c + hw_ + 5.5), (t_c - hw_ - 5.5, t_c - hw_ - 0.6)):
        cuboid(W + 0.4, s_tip - 2.5, t0, t1, base - 0.2, base + 0.75, stone_)
        cuboid(W + 0.6, s_tip - 2.7, t0 + 0.2, t1 - 0.2, base + 0.75, base + 1.45, hedge)
    stats["heracles"] = {"base": round(base, 2), "top": round(zr + 0.32, 2), "storeys": NF + 2, "bays": nb}
    return orient(Polygon([SW, SE, SE + D * v, SW + D * v]), 1.0)


# The two blocks either side of the slab, on the boulevard (Mapillary 2021
# and 2025): the same cream render and the same hand. South block (BD TOPO
# 074373, 7 levels, 22 m): shops behind stone piers on the ground floor
# (Planet Sushi), on the boulevard loggias with brown slatted parapets and
# blue awnings between windows, plain walls with small windows elsewhere
# (the entrance court side too), a pergola on the roof terrace
# under the slab. North block (074452, 6 levels, 19.5 m): an arcade of
# square stone pillars in front of the shops (Société Générale, Wally),
# loggias with blue awnings and windows with roller shutters and small iron
# balconies on the boulevard, small windows towards the court.
HERACLES_WINGS = {
    "BATIMENT0000000074074373": {"name": "south", "gf": 4.2, "fh": 2.95, "nf": 6, "ground": "shop",
                                 "parapet": "#8a5a3a"},
    "BATIMENT0000000074074452": {"name": "north", "gf": 4.4, "fh": 2.95, "nf": 5, "ground": "arcade",
                                 "parapet": HERACLES_WALL},
}
HERACLES_COURT = "BATIMENT0000000074074393"   # the planted entrance court, under the canopy
HERACLES_STONE = "#ddd2bd"                     # travertine piers and pillars


def _wing_edge_spec(name, mid, nrm, L):
    """What each wall of a side block shows, from where it faces."""
    east, north, south = nrm[0] > 0.75, nrm[1] > 0.75, nrm[1] < -0.75
    if name == "south":
        if east:                                        # the boulevard
            return "WLWLWLW"[:max(3, int(round(L / 3.4)))] if L > 9 else "S" * max(1, int(round(L / 3.2)))
    else:
        if east and L > 20:
            n = max(3, int(round(L / 3.45)))
            return ("WLL" * n)[:n - 1] + "W"
    if L < 2.5:
        return ""
    return "S" * max(1, int(round(L / 3.4)))


def heracles_wing(m, poly, key, mntR, stats):
    spec = HERACLES_WINGS[key]
    rnd = rng_for(key)
    ring = np.asarray(poly.exterior.coords)[:, :2]
    gz = mntR.sample(ring[:, 0], ring[:, 1])
    base = float(np.nanmin(gz)) if np.isfinite(gz).any() else 3.6
    GF, FH, NF = spec["gf"], spec["fh"], spec["nf"]
    ztop = base + GF + NF * FH
    wall = np.array(hexrgb(HERACLES_WALL))
    edge = np.minimum(wall * 1.04 + 6, 255)
    stone = np.array(hexrgb(HERACLES_STONE))
    par_c = np.array(hexrgb(spec["parapet"]))
    shutter = np.array(hexrgb("#e9e6de"))
    awning = np.array(hexrgb(HERACLES_AWNING))
    glass_c = np.array(hexrgb("#5b6a74"))
    DL = 1.2
    n_l = 0
    for A, B in zip(ring[:-1], ring[1:]):
        L = float(np.linalg.norm(B - A))
        if L < 0.5:
            continue
        d = (B - A) / L
        nrm = np.array([d[1], -d[0]])                  # outward (the ring runs anticlockwise)
        pat = _wing_edge_spec(spec["name"], (A + B) / 2, nrm, L)

        def E(s, dep, z):
            s, dep, z = np.broadcast_arrays(np.atleast_1d(np.asarray(s, float)),
                                            np.atleast_1d(np.asarray(dep, float)),
                                            np.atleast_1d(np.asarray(z, float)))
            q = A[None] + s[:, None] * d[None] + dep[:, None] * nrm[None]
            return enu_fast(q[:, 0], q[:, 1], z)

        def W(s, dep, z, ds, dd, dz):
            return E(s + ds, dep + dd, z + dz) - E(s, dep, z)

        def quad(s0, s1, d0, d1, z0, z1, col, face):
            """A quad from (s0, d0) to (s1, d1), z0 to z1, facing face
            (ds, ddep, dz); horizontal when z0 == z1 (then d0 to d1 is
            its depth)."""
            if z0 == z1:
                P = [E(s0, d0, z0), E(s1, d0, z0), E(s1, d1, z0), E(s0, d1, z0)]
            else:
                P = [E(s0, d0, z0), E(s1, d1, z0), E(s1, d1, z1), E(s0, d0, z1)]
            _quad(m, *P, col, W((s0 + s1) / 2, (d0 + d1) / 2, (z0 + z1) / 2, *face))

        def pane(s0, s1, dep, z0, z1, cell, fl):
            uvs = [np.array([[cell + 0.06, fl + 0.18]]), np.array([[cell + 0.94, fl + 0.18]]),
                   np.array([[cell + 0.94, fl + 0.9]]), np.array([[cell + 0.06, fl + 0.9]])]
            _quad_uv(m, E(s0, dep, z0), E(s1, dep, z0), E(s1, dep, z1), E(s0, dep, z1), glass_c,
                     W((s0 + s1) / 2, dep, z0, 0, 1, 0), uvs, kind=WALL_GLASS, style=S_GLASS)

        OUT = (0, 1, 0)
        # Ground floor.
        nb = max(1, len(pat))
        bay = L / nb
        g0, g1 = base - 1.0, base + GF
        if pat and spec["ground"] == "arcade" and L > 20:
            AD = 2.4
            quad(0, L, -AD, -AD, g0, g1, wall * 0.9, OUT)                    # back wall
            for k in range(nb):
                pane(k * bay + 0.35, (k + 1) * bay - 0.35, -AD + 0.02, base + 0.1, g1 - 0.7, k, 1)
            _quad(m, E(0, -AD, g1), E(L, -AD, g1), E(L, 0, g1), E(0, 0, g1), wall * 0.8,
                  W(L / 2, -1, g1, 0, 0, -1))                                # soffit
            quad(0, 0, -AD, 0, g0, g1, wall * 0.9, (1, 0, 0))                # ends of the arcade
            quad(L, L, -AD, 0, g0, g1, wall * 0.9, (-1, 0, 0))
            for k in range(nb + 1):
                sc = min(max(k * bay, 0.35), L - 0.35)
                for (a0, a1, b0, b1, f) in ((sc - 0.3, sc + 0.3, 0, 0, OUT), (sc - 0.3, sc + 0.3, -0.6, -0.6, (0, -1, 0)),
                                            (sc - 0.3, sc - 0.3, -0.6, 0, (-1, 0, 0)), (sc + 0.3, sc + 0.3, -0.6, 0, (1, 0, 0))):
                    quad(a0, a1, b0, b1, g0, g1, stone, f)
        elif pat and pat != "S" * len(pat):
            quad(0, L, 0, 0, g0, g1, stone, OUT)
            for k in range(nb):
                pane(k * bay + 0.3, (k + 1) * bay - 0.3, 0.03, base + 0.1, g1 - 0.75, k, 1)
            # A thin canopy over the shop windows.
            _quad(m, E(0, 0, g1 - 0.6), E(L, 0, g1 - 0.6), E(L, 1.1, g1 - 0.6), E(0, 1.1, g1 - 0.6), edge,
                  W(L / 2, 0.5, g1 - 0.6, 0, 0, 1))
            _quad(m, E(0, 0, g1 - 0.68), E(L, 0, g1 - 0.68), E(L, 1.1, g1 - 0.68), E(0, 1.1, g1 - 0.68), edge * 0.8,
                  W(L / 2, 0.5, g1 - 0.68, 0, 0, -1))
            quad(0, L, 1.1, 1.1, g1 - 0.68, g1 - 0.6, edge, OUT)
        else:
            quad(0, L, 0, 0, g0, g1, wall, OUT)
        # Storeys.
        for i in range(1, NF + 1):
            z0, z1 = base + GF + (i - 1) * FH, base + GF + i * FH
            if not pat:
                quad(0, L, 0, 0, z0, z1, wall, OUT)
                continue
            for k, c in enumerate(pat):
                s0, s1 = k * bay, (k + 1) * bay
                if c == "L":
                    p = 0.35
                    a, b = s0 + p, s1 - p
                    quad(s0, a, 0, 0, z0, z1, wall, OUT)
                    quad(b, s1, 0, 0, z0, z1, wall, OUT)
                    quad(a, b, 0, 0, z1 - 0.22, z1, edge, OUT)               # slab edge
                    quad(a, b, -DL, -DL, z0, z1 - 0.22, wall * 0.88, OUT)    # back of the loggia
                    quad(a, a, -DL, 0, z0, z1 - 0.22, wall * 0.84, (1, 0, 0))
                    quad(b, b, -DL, 0, z0, z1 - 0.22, wall * 0.84, (-1, 0, 0))
                    quad(a, b, -DL, 0, z1 - 0.22, z1 - 0.22, wall * 0.8, (0, 0, -1))
                    quad(a, b, -DL, 0, z0, z0, wall * 0.9, (0, 0, 1))
                    pane(a + 0.15, b - 0.15, -DL + 0.02, z0 + 0.1, z1 - 0.5, k, i)
                    # Parapet: solid, cream or brown slats.
                    quad(a, b, 0.02, 0.02, z0, z0 + 1.0, par_c, OUT)
                    quad(a, b, -0.12, -0.12, z0, z0 + 1.0, par_c * 0.85, (0, -1, 0))
                    quad(a, b, -0.12, 0.02, z0 + 1.0, z0 + 1.0, np.minimum(par_c * 1.08, 255), (0, 0, 1))
                    if rnd.random() < 0.6:
                        za, zb_ = z1 - 0.3, z1 - 1.05
                        P0, P1 = E([a + 0.03, b - 0.03], -0.05, za), E([b - 0.03, a + 0.03], 0.6, zb_)
                        for sg in (1, -1):
                            _quad(m, P0[[0]], P0[[1]], P1[[0]], P1[[1]], awning,
                                  W((a + b) / 2, 0.3, za, 0, 0.6 * sg, 0.75 * sg))
                    n_l += 1
                else:
                    quad(s0, s1, 0, 0, z0, z1, wall, OUT)
                    sc = (s0 + s1) / 2
                    if c == "S":
                        pane(sc - 0.45, sc + 0.45, 0.02, z0 + 1.0, z0 + 2.15, k, i)
                    else:
                        hw = min(0.8, bay * 0.28)
                        pane(sc - hw, sc + hw, 0.02, z0 + 0.15, z0 + 2.35, k, i)
                        r = rnd.random()
                        if r < 0.7:                                          # roller shutter part way down
                            zs = z0 + 2.35 - (2.2 * rnd.uniform(0.15, 1.0))
                            quad(sc - hw - 0.03, sc + hw + 0.03, 0.04, 0.04, zs, z0 + 2.4, shutter, OUT)
                        if spec["name"] == "north" and rnd.random() < 0.5:
                            # A small iron balcony.
                            _quad(m, E(sc - hw - 0.2, 0, z0), E(sc + hw + 0.2, 0, z0), E(sc + hw + 0.2, 0.55, z0),
                                  E(sc - hw - 0.2, 0.55, z0), edge, W(sc, 0.3, z0, 0, 0, 1))
                            q0, q1 = A + d * (sc - hw - 0.2) + nrm * 0.55, A + d * (sc + hw + 0.2) + nrm * 0.55
                            railing(m, q0[None], q1[None], z0, "#23272a", h=1.0)
        # Cornice band at the top.
        quad(0, L, 0.12, 0.12, ztop - 0.3, ztop, edge, OUT)
    # Roof and parapet.
    add_roof(m, poly, ztop, np.array(hexrgb("#d3ccbd")), False)
    pc = edge * 0.97
    add_walls(m, poly, ztop, ztop + 0.9, ztop, 10, pc, PLAIN)
    add_walls(m, poly, ztop, ztop + 0.9, ztop, 10, pc * 0.85, PLAIN, inward=True)
    inner = poly.buffer(-0.25, join_style=2)
    for rp in to_polys(poly.difference(inner) if not inner.is_empty else poly):
        add_roof(m, rp, ztop + 0.9, pc * 1.05, False)
    if spec["name"] == "south":
        # The pergola and planters on the roof terrace under the slab's south end.
        ax = np.array([0.99, 0.17])
        iron = np.array(hexrgb("#2b2d2c"))
        cx, cy, w, dd = 1056046.0, 6302806.5, 11.0, 4.6
        side = np.array([-ax[1], ax[0]])
        for i_ in (-1, 0, 1):
            for j_ in (-1, 1):
                q = np.array([cx, cy]) + ax * i_ * (w / 2 - 0.1) + side * j_ * (dd / 2 - 0.1)
                _box(m, q[0], q[1], ax, 0.12, 0.12, ztop, ztop + 2.6, iron)
        for j_ in (-1, 1):
            q = np.array([cx, cy]) + side * j_ * (dd / 2 - 0.1)
            _box(m, q[0], q[1], ax, w, 0.14, ztop + 2.45, ztop + 2.6, iron)
        for i_ in np.linspace(-w / 2 + 0.3, w / 2 - 0.3, 12):
            q = np.array([cx, cy]) + ax * i_
            _box(m, q[0], q[1], side, dd, 0.08, ztop + 2.6, ztop + 2.7, iron)
        for i_ in (-0.38, -0.1, 0.2, 0.42):
            q = np.array([cx, cy]) + ax * i_ * w - side * (dd / 2 - 0.5)
            _box(m, q[0], q[1], ax, 1.0, 0.8, ztop, ztop + 0.6, np.array(hexrgb("#b9a789")))
            add_tree(m, q[0], q[1], ztop + 0.6, rnd.uniform(1.2, 2.0), rnd.uniform(0.6, 0.9), "broad", rnd)
    stats.setdefault("heracles_wings", {})[spec["name"]] = {"base": round(base, 2), "top": round(ztop, 2),
                                                            "loggias": n_l}
    return poly


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


# ------------------------------------------------------------- Le Rocher ----
# Monaco-Ville on its rock. The Prince's Palace and the Oceanographic Museum
# are built piece by piece, like the Casino: the IGN data cannot carry them
# alone (IGN blurs its photo over the Palace, and the LiDAR surface there is
# a simplified model: flat wings, no towers). Their plans come from BD TOPO
# and that surface (wing roofs, the Cour d'honneur), their faces from street
# photos on Mapillary (2015-2025): the Palace's front on Place du Palais,
# the Museum's entrance and its north-east wing on Avenue Saint-Martin.
# Sides nobody photographed (the Palace's cliff side, the Museum's sea face)
# follow the same architecture and are noted as such.

class Frame:
    """A building's own frame: s along a main face, t out of it (toward the
    viewer standing in front), z height (m NGF). Boxes, glass, arches,
    columns and battlements in those coordinates."""

    def __init__(self, m, O, u):
        self.m = m
        self.O = np.asarray(O, float)
        u = np.asarray(u, float)
        self.u = u / np.linalg.norm(u)
        self.v = np.array([self.u[1], -self.u[0]])      # out of the main face

    def xy(self, s, t):
        return self.O + s * self.u + t * self.v

    def E(self, s, t, z):
        s, t, z = np.broadcast_arrays(np.atleast_1d(np.asarray(s, float)), np.atleast_1d(np.asarray(t, float)),
                                      np.atleast_1d(np.asarray(z, float)))
        q = self.O[None] + s[:, None] * self.u[None] + t[:, None] * self.v[None]
        return enu_fast(q[:, 0], q[:, 1], z)

    def want(self, s, t, z, ds, dt, dz):
        return self.E(s + ds, t + dt, z + dz) - self.E(s, t, z)

    def poly(self, s0, s1, t0, t1):
        return orient(Polygon([self.xy(s0, t0), self.xy(s1, t0), self.xy(s1, t1), self.xy(s0, t1)]), 1.0)

    def quad(self, a, b, c, d, col, n, style=None, uvm=None):
        """a..d: (s, t, z) corners; n: (ds, dt, dz) the side it faces. With
        uvm, texture coordinates in metres (along, up) times uvm."""
        P = [self.E(*p) for p in (a, b, c, d)]
        ctr = np.mean([np.array(p, float) for p in (a, b, c, d)], 0)
        w = self.want(*ctr, *n)
        col = np.asarray(col, float)
        if uvm is None and style is None:
            _quad(self.m, *P, col, w)
            return
        A = np.array([a, b, c, d], float)
        # Along: the horizontal distance from the first corner; up: z.
        along = np.hypot(A[:, 0] - A[0, 0], A[:, 1] - A[0, 1])
        k = 1.0 if uvm is None else uvm
        uv = [np.array([[along[i] * k, A[i, 2] * k]]) for i in range(4)]
        _quad_uv(self.m, *P, col, w, uv, style=style)

    def box(self, s0, s1, t0, t1, z0, z1, col, top=True, bottom=False, sides="stST", style=None, uvm=None):
        """sides: which walls to draw: s (at s0), S (s1), t (t0), T (t1)."""
        s0, s1 = min(s0, s1), max(s0, s1)
        t0, t1 = min(t0, t1), max(t0, t1)
        if "s" in sides:
            self.quad((s0, t0, z0), (s0, t1, z0), (s0, t1, z1), (s0, t0, z1), col, (-1, 0, 0), style, uvm)
        if "S" in sides:
            self.quad((s1, t0, z0), (s1, t1, z0), (s1, t1, z1), (s1, t0, z1), col, (1, 0, 0), style, uvm)
        if "t" in sides:
            self.quad((s0, t0, z0), (s1, t0, z0), (s1, t0, z1), (s0, t0, z1), col, (0, -1, 0), style, uvm)
        if "T" in sides:
            self.quad((s0, t1, z0), (s1, t1, z0), (s1, t1, z1), (s0, t1, z1), col, (0, 1, 0), style, uvm)
        if top:
            self.quad((s0, t0, z1), (s1, t0, z1), (s1, t1, z1), (s0, t1, z1), col * 1.02, (0, 0, 1))
        if bottom:
            self.quad((s0, t0, z0), (s1, t0, z0), (s1, t1, z0), (s0, t1, z0), col * 0.8, (0, 0, -1))

    # Faces: "+t" (t = const, facing +t), "-t", "+s", "-s". A point on a face:
    # a = the coordinate along it, the face's own plane coordinate c.
    def _on(self, face, a, c):
        return (c, a) if face[1] == "s" else (a, c)

    def _n(self, face):
        sg = 1.0 if face[0] == "+" else -1.0
        return (sg, 0, 0) if face[1] == "s" else (0, sg, 0)

    def face_quad(self, face, c, a0, a1, z0, z1, col, out=0.0, style=None, uvm=None):
        sg = 1.0 if face[0] == "+" else -1.0
        cc = c + sg * out
        p = [(*self._on(face, a0, cc), z0), (*self._on(face, a1, cc), z0), (*self._on(face, a1, cc), z1),
             (*self._on(face, a0, cc), z1)]
        self.quad(*p, col, self._n(face), style, uvm)

    def slab(self, face, c, a0, a1, z0, z1, depth, col, sides=True):
        """A block standing proud of a face by `depth` (bands, pilasters,
        cornices, surrounds): its front, top, bottom and ends."""
        sg = 1.0 if face[0] == "+" else -1.0
        if face[1] == "t":
            self.box(a0, a1, c, c + sg * depth, z0, z1, col, top=True, bottom=True,
                     sides=("sS" if sides else "") + ("T" if sg > 0 else "t"))
        else:
            self.box(c, c + sg * depth, a0, a1, z0, z1, col, top=True, bottom=True,
                     sides=("tT" if sides else "") + ("S" if sg > 0 else "s"))

    def glass(self, face, c, a0, a1, z0, z1, cell, floor_, col="#5b6a74"):
        """A window: the viewer's reflective glass, lit at random at night."""
        uvs = [np.array([[cell + 0.06, floor_ + 0.18]]), np.array([[cell + 0.94, floor_ + 0.18]]),
               np.array([[cell + 0.94, floor_ + 0.9]]), np.array([[cell + 0.06, floor_ + 0.9]])]
        sg = 1.0 if face[0] == "+" else -1.0
        cc = c + sg * 0.03
        P = [self.E(*self._on(face, a0, cc), z0), self.E(*self._on(face, a1, cc), z0),
             self.E(*self._on(face, a1, cc), z1), self.E(*self._on(face, a0, cc), z1)]
        ctr = ((a0 + a1) / 2, cc)
        _quad_uv(self.m, *P, np.array(hexrgb(col)), self.want(*self._on(face, *ctr), (z0 + z1) / 2, *self._n(face)),
                 uvs, kind=WALL_GLASS, style=S_GLASS)

    def arch_glass(self, face, c, a, w, zs, cell, floor_, col="#5b6a74", nseg=8, dark=False, th1=math.pi):
        """The round head of an arched window (springing at zs), as a fan
        (th1 = 2 pi: a round window centred at zs)."""
        sg = 1.0 if face[0] == "+" else -1.0
        cc = c + sg * 0.03
        th = np.linspace(0, th1, nseg + 1)
        r = w / 2
        ctr = self.E(*self._on(face, a, cc), zs)
        pts = [self.E(*self._on(face, a + r * math.cos(x), cc), zs + r * math.sin(x)) for x in th]
        n_ = self.want(*self._on(face, a, cc), zs, *self._n(face))
        colv = np.array(hexrgb(col), float)
        for i in range(nseg):
            A_, B_ = pts[i], pts[i + 1]
            nn = np.cross(A_ - ctr, B_ - ctr)
            if (nn * n_).sum() < 0:
                A_, B_ = B_, A_
            if dark:
                self.m.tri(ctr, A_, B_, colv, PLAIN)
            else:
                uv = np.array([[[cell + 0.5, floor_ + 0.6], [cell + 0.1, floor_ + 0.85], [cell + 0.9, floor_ + 0.85]]])
                self.m.tri(ctr, A_, B_, colv, WALL_GLASS, uv=uv, style=S_GLASS)

    def arch_ring(self, face, c, a, w, zs, depth, band, col, nseg=12, th1=math.pi):
        """The moulded surround of an arch: a half ring standing proud of
        the face by depth, with its outer edge returned to the wall
        (th1 = 2 pi: the ring of a round window)."""
        sg = 1.0 if face[0] == "+" else -1.0
        r0, r1 = w / 2, w / 2 + band
        th = np.linspace(0, th1, nseg + 1)
        cf = c + sg * depth
        n = self._n(face)
        col = np.asarray(col, float)
        for i in range(nseg):
            x0, x1 = th[i], th[i + 1]
            pin0 = (*self._on(face, a + r0 * math.cos(x0), cf), zs + r0 * math.sin(x0))
            pin1 = (*self._on(face, a + r0 * math.cos(x1), cf), zs + r0 * math.sin(x1))
            pout0 = (*self._on(face, a + r1 * math.cos(x0), cf), zs + r1 * math.sin(x0))
            pout1 = (*self._on(face, a + r1 * math.cos(x1), cf), zs + r1 * math.sin(x1))
            self.quad(pin0, pin1, pout1, pout0, col * 1.02, n)
            # The outer return and the inner reveal, back to the wall.
            for rr, sh in ((r1, 0.9), (r0, 0.82)):
                q0 = (*self._on(face, a + rr * math.cos(x0), c), zs + rr * math.sin(x0))
                q1 = (*self._on(face, a + rr * math.cos(x1), c), zs + rr * math.sin(x1))
                f0 = (*self._on(face, a + rr * math.cos(x0), cf), zs + rr * math.sin(x0))
                f1 = (*self._on(face, a + rr * math.cos(x1), cf), zs + rr * math.sin(x1))
                xm = (x0 + x1) / 2
                dn = (math.cos(xm), math.sin(xm)) if rr == r1 else (-math.cos(xm), -math.sin(xm))
                nn = (dn[0], 0, dn[1]) if face[1] == "t" else (0, dn[0], dn[1])
                self.quad(q0, q1, f1, f0, col * sh, nn)

    def window(self, face, c, a, w, z0, z1, cell, floor_, frame=None, arch=False, depth=0.12, band=0.22,
               sill=True, pediment=False, shutters=None, dark=False):
        """A window centred at a on a face: glass, a white surround, a sill,
        an arched head (arch=True: z1 is the springing line), a little
        pediment, shutters."""
        if dark:
            self.face_quad(face, c, a - w / 2, a + w / 2, z0, z1, np.array(hexrgb("#2a2622")), out=0.03)
        else:
            self.glass(face, c, a - w / 2, a + w / 2, z0, z1, cell, floor_)
        if arch:
            self.arch_glass(face, c, a, w, z1, cell, floor_, dark=dark)
        if frame is not None:
            self.slab(face, c, a - w / 2 - band, a - w / 2, z0, z1, depth, frame)
            self.slab(face, c, a + w / 2, a + w / 2 + band, z0, z1, depth, frame)
            if arch:
                self.arch_ring(face, c, a, w, z1, depth, band, frame)
            else:
                self.slab(face, c, a - w / 2 - band, a + w / 2 + band, z1, z1 + band, depth, frame)
            if sill:
                self.slab(face, c, a - w / 2 - band - 0.08, a + w / 2 + band + 0.08, z0 - 0.14, z0, depth + 0.08, frame)
            if pediment:
                zp = (z1 + w / 2 + band) if arch else (z1 + band)
                self.slab(face, c, a - w / 2 - band - 0.15, a + w / 2 + band + 0.15, zp + 0.05, zp + 0.22, depth + 0.1,
                          frame)
                self.slab(face, c, a - w * 0.3, a + w * 0.3, zp + 0.22, zp + 0.42, depth + 0.05, frame)
        if shutters is not None:
            for sg in (-1, 1):
                a0 = a + sg * (w / 2 + band + 0.02)
                a1 = a0 + sg * w / 2
                self.face_quad(face, c, min(a0, a1), max(a0, a1), z0, z1 + (w / 2 if arch else 0.0) * 0.6,
                               shutters, out=0.05)

    def column(self, s, t, z0, z1, r, col, sides=12, capital=None):
        x, y = self.xy(s, t)
        _cyl(self.m, x, y, z0, z1, r, col, sides)
        cap = col if capital is None else capital
        p = self.xy(s, t)
        _box(self.m, p[0], p[1], self.u, 2.5 * r, 2.5 * r, z1 - 0.05, z1 + 0.35 * r * 2, cap)
        _box(self.m, p[0], p[1], self.u, 2.4 * r, 2.4 * r, z0, z0 + 0.4 * r, cap)

    def battlements(self, s0, s1, t0, t1, z, h, col, merlon=1.0, gap=0.7, th=0.45, corbel=None):
        """A parapet with merlons round a rectangle (top of a tower or a
        wall), optionally on a corbelled machicolation band."""
        for (a0, a1, c, face) in ((s0, s1, t0, "-t"), (s0, s1, t1, "+t"), (t0, t1, s0, "-s"), (t0, t1, s1, "+s")):
            if c is None:
                continue
            L = a1 - a0
            n = max(1, int(round((L + gap) / (merlon + gap))))
            pitch = (L + gap) / n
            sg = 1.0 if face[0] == "+" else -1.0
            o = (corbel or 0.0)
            # Parapet wall, the merlons on it.
            if face[1] == "t":
                self.box(a0 - (o if True else 0), a1 + o, c - sg * th + sg * o, c + sg * o, z, z + h * 0.45, col)
            else:
                self.box(c - sg * th + sg * o, c + sg * o, a0 - o, a1 + o, z, z + h * 0.45, col)
            for k in range(n):
                b0 = a0 - o + k * pitch * (L + 2 * o) / L
                b1 = b0 + merlon
                if face[1] == "t":
                    self.box(b0, min(b1, a1 + o), c - sg * th + sg * o, c + sg * o, z + h * 0.45, z + h, col)
                else:
                    self.box(c - sg * th + sg * o, c + sg * o, b0, min(b1, a1 + o), z + h * 0.45, z + h, col)
            if corbel:
                # Corbels: small brackets under the overhang.
                nc = max(1, int(L / 0.9))
                for k in range(nc):
                    a_ = a0 + (k + 0.5) * L / nc
                    if face[1] == "t":
                        self.box(a_ - 0.2, a_ + 0.2, c, c + sg * o, z - 1.1, z, col * 0.95)
                    else:
                        self.box(c, c + sg * o, a_ - 0.2, a_ + 0.2, z - 1.1, z, col * 0.95)

    def tri(self, a, b, c, col, n, kind=PLAIN):
        """One triangle of (s, t, z) corners, facing n = (ds, dt, dz)."""
        P = [self.E(*p) for p in (a, b, c)]
        w = self.want(*np.mean(np.array([a, b, c], float), 0), *n)
        if (np.cross(P[1] - P[0], P[2] - P[0]) * w).sum() < 0:
            P[1], P[2] = P[2], P[1]
        self.m.tri(P[0], P[1], P[2], np.asarray(col, float), kind)

    def gable_roof(self, s0, s1, t0, t1, z, rise, col, wall=None, along="t", over=0.4):
        """A pitched roof, its ridge along t (along="t") or s, eaves at z,
        and, with wall, the two triangular gable ends in that colour."""
        col = np.asarray(col, float)

        def rq(a, b, c, d, cc, n):
            self.tri(a, b, c, cc, n, ROOF_TILE)
            self.tri(a, c, d, cc, n, ROOF_TILE)
        if along == "t":
            sm = (s0 + s1) / 2
            for se, sg in ((s0 - over, -1), (s1 + over, 1)):
                ze = z - over * rise / ((s1 - s0) / 2)
                rq((se, t0 - over, ze), (se, t1 + over, ze), (sm, t1 + over, z + rise), (sm, t0 - over, z + rise),
                          col * (1.0 if sg < 0 else 0.94), (sg * rise, 0, (s1 - s0) / 2))
            if wall is not None:
                for tt, sg in ((t0, -1), (t1, 1)):
                    self.tri((s0, tt, z), (s1, tt, z), (sm, tt, z + rise), wall, (0, sg, 0))
        else:
            tm = (t0 + t1) / 2
            for te, sg in ((t0 - over, -1), (t1 + over, 1)):
                ze = z - over * rise / ((t1 - t0) / 2)
                rq((s0 - over, te, ze), (s1 + over, te, ze), (s1 + over, tm, z + rise), (s0 - over, tm, z + rise),
                          col * (1.0 if sg < 0 else 0.94), (0, sg * rise, (t1 - t0) / 2))
            if wall is not None:
                for ss, sg in ((s0, -1), (s1, 1)):
                    self.tri((ss, t0, z), (ss, t1, z), (ss, tm, z + rise), wall, (sg, 0, 0))

    def _ring_pts(self, s, t, r, n, th0, th1):
        th = np.linspace(th0, th1, n + 1)
        return [(s + r * math.cos(x), t + r * math.sin(x)) for x in th], th

    def drum(self, s, t, z0, z1, r, col, n=12, th0=0.0, th1=2 * math.pi, style=None, uvm=None):
        """A polygonal wall round (s, t): the round turrets, the apse."""
        pts, th = self._ring_pts(s, t, r, n, th0, th1)
        for i in range(n):
            xm = (th[i] + th[i + 1]) / 2
            (a0, b0), (a1, b1) = pts[i], pts[i + 1]
            self.quad((a0, b0, z0), (a1, b1, z0), (a1, b1, z1), (a0, b0, z1), col, (math.cos(xm), math.sin(xm), 0),
                      style, uvm)

    def cone(self, s, t, z, r, h, col, n=12, th0=0.0, th1=2 * math.pi, kind=ROOF_TILE):
        """A conical (n = 4, 8: pyramidal) roof, or a fan of it."""
        pts, th = self._ring_pts(s, t, r, n, th0, th1)
        for i in range(n):
            xm = (th[i] + th[i + 1]) / 2
            (a0, b0), (a1, b1) = pts[i], pts[i + 1]
            self.tri((a0, b0, z), (a1, b1, z), (s, t, z + h), np.asarray(col, float) * (0.94 + 0.06 * math.sin(xm)),
                     (math.cos(xm) * h, math.sin(xm) * h, r), kind)

    def dome(self, s, t, z, r, h, col, n=12, rings=5, kind=PLAIN):
        """A round dome (the turrets' stone caps)."""
        prof = [(math.cos(k / rings * math.pi / 2), math.sin(k / rings * math.pi / 2)) for k in range(rings + 1)]
        th = np.linspace(0, 2 * math.pi, n + 1)
        col = np.asarray(col, float)
        for j in range(rings):
            (ra, za), (rb, zb) = prof[j], prof[j + 1]
            for i in range(n):
                xm = (th[i] + th[i + 1]) / 2
                pa = [(s + r * ra * math.cos(x), t + r * ra * math.sin(x), z + h * za) for x in (th[i], th[i + 1])]
                pb = [(s + r * rb * math.cos(x), t + r * rb * math.sin(x), z + h * zb) for x in (th[i], th[i + 1])]
                nz = (ra + rb) / 2
                nrm = (math.cos(xm) * nz, math.sin(xm) * nz, (za + zb) / 2 * r / max(h, 0.1))
                cc = col * (0.9 + 0.1 * (za + zb) / 2)
                self.tri(pa[0], pa[1], pb[1], cc, nrm, kind)
                if rb > 1e-6:
                    self.tri(pa[0], pb[1], pb[0], cc, nrm, kind)

    def hip_roof(self, s0, s1, t0, t1, z, col, overhang=0.5, pitch=19):
        poly = self.poly(s0 - overhang, s1 + overhang, t0 - overhang, t1 + overhang)
        slope = math.tan(math.radians(pitch))
        t = skeleton_roof(poly, z, slope, 99 / slope, dt=0.4)
        if t is None:
            return
        v, zz, tri = t
        P = enu_fast(v[:, 0], v[:, 1], zz)
        self.m.tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], np.asarray(col, float), ROOF_TILE)
        # Soffit under the overhang.
        self.box(s0 - overhang, s1 + overhang, t0 - overhang, t1 + overhang, z - 0.18, z, col * 0.0 + np.array(hexrgb("#e8e0cc")),
                 top=False, bottom=True)

    def balustrade(self, face, c, a0, a1, z, col, h=1.0, depth=0.32, plinth=0.18, step=0.3):
        """A balustrade on a face's edge (c: the plane its front sits on)."""
        sg = 1.0 if face[0] == "+" else -1.0
        cb = c - sg * depth
        self.slab(face, cb, a0, a1, z, z + plinth, depth, col)
        nb = max(1, int((a1 - a0) / step))
        for k in range(nb):
            a = a0 + (k + 0.5) * (a1 - a0) / nb
            self.slab(face, cb + sg * depth * 0.25, a - 0.07, a + 0.07, z + plinth, z + h - 0.14, depth * 0.5, col * 1.02)
        self.slab(face, cb - sg * 0.02, a0 - 0.02, a1 + 0.02, z + h - 0.14, z + h, depth + 0.04, col)


def _cyl(m, x, y, z0, z1, r, col, sides=10):
    """A thin round shaft (columns, bollards, masts): _column without the
    wall builder's 30 cm shortest edge."""
    a = np.linspace(0, 2 * math.pi, sides + 1)
    ring = [(x + r * math.cos(t), y + r * math.sin(t)) for t in a[:-1]]
    add_walls(m, orient(Polygon(ring), 1.0), z0, z1, z0, 100, col, PLAIN, min_edge=0.005)


def _ground_min(F, mntR, s0, s1, t0, t1, n=6):
    ss, tt = np.meshgrid(np.linspace(s0, s1, n), np.linspace(t0, t1, n))
    p = F.O[None] + ss.ravel()[:, None] * F.u[None] + tt.ravel()[:, None] * F.v[None]
    z = mntR.sample(p[:, 0], p[:, 1])
    return float(np.nanmin(z)) if np.isfinite(z).any() else None


# --------------------------------------------------- the Prince's Palace ----
PALACE = "BATIMENT0000000074079000"
PALACE_O = (1056035.4, 6302411.0)          # where the square front meets its step (BD TOPO corner)
PALACE_U = (31.9, 35.9)                    # along the front on Place du Palais, south-west to north-east
PALACE_WALL = "#efe3c6"                    # pale buttery ochre render (street photos)
PALACE_TRIM = "#f8f3e7"                    # white window surrounds, bands, balustrades
PALACE_MARBLE = "#f4f2ec"                  # the main gate's white marble
PALACE_GREY = "#bcb9b0"                    # Tour Sainte-Marie's grey stone
PALACE_ROOF = "#ae6a52"                    # terracotta


def palais_princier(m, gm, mntR, stats):
    """The Prince's Palace, in a frame on its front: s along Place du
    Palais from the step in the front (0) to the north (+), t out onto the
    square, z m NGF. Plan from BD TOPO and the LiDAR roofs (the court is
    s 0..23, t -22..-63); the front from the 2019 Mapillary photos."""
    F = Frame(m, PALACE_O, PALACE_U)
    zq = mntR.sample(np.array([F.xy(13.5, 6.0)[0]]), np.array([F.xy(13.5, 6.0)[1]]))[0]
    Z = float(zq) if np.isfinite(zq) else 62.0          # the square
    wall = np.array(hexrgb(PALACE_WALL), float)
    trim = np.array(hexrgb(PALACE_TRIM), float)
    marble = np.array(hexrgb(PALACE_MARBLE), float)
    grey = np.array(hexrgb(PALACE_GREY), float)
    roof = np.array(hexrgb(PALACE_ROOF), float)
    court_wall = np.array(hexrgb("#ecd3a4"), float)     # the court's frescoed ochre
    dark = np.array(hexrgb("#2c2824"), float)
    n_win = 0

    def gmin(s0, s1, t0, t1):
        g = _ground_min(F, mntR, s0, s1, t0, t1)
        return min(Z - 0.5, g - 0.5) if g is not None else Z - 0.5

    def wing(s0, s1, t0, t1, eave, faces, rows=None, bay=3.6, roof_=True, arch_top=False, colw=None):
        """A plain wing: walls from the ground (it drops to the cliffs on
        the outside), a cornice, a hipped tiled roof, rows of windows with
        white surrounds on the listed faces."""
        nonlocal n_win
        cw = wall if colw is None else colw
        zb = gmin(s0, s1, t0, t1)
        F.box(s0, s1, t0, t1, zb, Z + eave, cw, top=not roof_)
        for face in ("+t", "-t", "+s", "-s"):
            c = {"+t": t1, "-t": t0, "+s": s1, "-s": s0}[face]
            a0, a1 = (s0, s1) if face[1] == "t" else (t0, t1)
            F.slab(face, c, a0 - 0.15, a1 + 0.15, Z + eave - 0.7, Z + eave, 0.3, trim)   # cornice
            if face not in faces:
                continue
            # Floors: the palace storeys above the square, more down the cliff.
            g = _ground_min(F, mntR, *((a0, a1, c, c) if face[1] == "t" else (c, c, a0, a1)))
            g = Z if g is None else g
            zs = rows if rows is not None else [z for z in np.arange(Z + eave - 4.2, g + 1.5, -4.0)]
            nb = max(1, int(round((a1 - a0) / bay)))
            for i, z_ in enumerate(zs):
                for k in range(nb):
                    a = a0 + (k + 0.5) * (a1 - a0) / nb
                    gz = _ground_min(F, mntR, *((a, a, c, c) if face[1] == "t" else (c, c, a, a)), n=2)
                    if gz is not None and z_ < gz + 0.8:
                        continue
                    F.window(face, c, a, 1.25, z_, z_ + 2.1, k, i + 1, frame=trim, arch=arch_top and i == 0,
                             shutters=None)
                    n_win += 1
        if roof_:
            F.hip_roof(s0, s1, t0, t1, Z + eave, roof, overhang=0.45)

    # -- The front on the square ------------------------------------------
    # Gate block (7 bays) and loggia block (5 bays + one), on a plinth that
    # runs on to the south as the terrace wall with its balustrade.
    PL = 6.3                                    # plinth (ground floor) height
    GB = (0.0, 27.0)                            # gate block
    LG = (27.0, 45.6)                           # the loggias
    TL = (45.6, 48.0)                           # one narrow bay before the grey tower
    zf = gmin(0, 48, -21, 0)
    F.box(0.0, 48.0, -21.0, 0.0, zf, Z + PL, wall, top=False)
    F.box(GB[0], GB[1], -21.0, 0.0, Z + PL, Z + 16.4, wall, top=False)
    F.box(LG[0], TL[1], -21.0, 0.0, Z + PL, Z + 19.4, wall, top=False)
    # Plinth: small square windows with little pediments, every bay.
    for a0, a1, nb in ((GB[0], GB[1], 7), (LG[0], LG[1], 5)):
        for k in range(nb):
            a = a0 + (k + 0.5) * (a1 - a0) / nb
            if a0 == GB[0] and k == 3:
                continue                         # the gate
            F.window("+t", 0.0, a, 0.95, Z + 2.5, Z + 3.85, k, 0, frame=trim, pediment=True)
            n_win += 1
    F.slab("+t", 0.0, -52.0, 48.0, Z - 0.3, Z + 0.45, 0.18, wall * 0.93)                 # base course
    F.slab("+t", 0.0, -0.2, 48.0, Z + PL - 0.35, Z + PL, 0.28, trim)                     # band under the terrace
    F.balustrade("+t", 0.0, 0.0, 12.0, Z + PL, trim, h=0.95)
    F.balustrade("+t", 0.0, 15.0, LG[0], Z + PL, trim, h=0.95)
    F.balustrade("+t", 0.0, LG[0], TL[1], Z + PL, trim, h=0.95)
    # Gate block: square windows, then the piano nobile's tall arched windows
    # each on its own little balustrade, then the cornice.
    for k in range(7):
        a = GB[0] + (k + 0.5) * (GB[1] - GB[0]) / 7
        if k != 3:
            F.window("+t", 0.0, a, 1.25, Z + 7.55, Z + 9.2, k, 1, frame=trim)
        F.balustrade("+t", 0.0, a - 1.05, a + 1.05, Z + 10.0, trim, h=0.75, depth=0.25)
        F.window("+t", 0.0, a, 1.5, Z + 10.75, Z + 13.25, k, 2, frame=trim, arch=True, band=0.26)
        F.slab("+t", 0.0, a - 1.85, a - 1.65, Z + 7.2, Z + 15.4, 0.12, trim)              # pilaster strips
        n_win += 2
    F.slab("+t", 0.0, GB[1] - 0.2, GB[1] + 0.2, Z + 7.2, Z + 15.4, 0.12, trim)
    F.slab("+t", 0.0, GB[0] - 0.3, GB[1], Z + 15.4, Z + 16.4, 0.4, trim)                 # cornice
    F.slab("+t", 0.0, GB[0] - 0.3, GB[1], Z + 15.0, Z + 15.4, 0.22, trim)
    # Loggia block: two tiers of arches (the lower three open, a balustrade in
    # each), a row of small windows above, a deeper cornice.
    for k in range(5):
        a = LG[0] + (k + 0.5) * (LG[1] - LG[0]) / 5
        bw = (LG[1] - LG[0]) / 5
        F.slab("+t", 0.0, a - bw / 2, a - bw / 2 + 0.55, Z + 7.0, Z + 17.6, 0.18, trim)  # piers
        for tier, (z0, zs, w) in enumerate(((Z + 7.6, Z + 9.9, 2.55), (Z + 12.0, Z + 14.7, 2.75))):
            open_ = tier == 0 and k >= 2
            if open_:
                # An open arch: dark loggia behind, a balustrade across it.
                F.window("+t", 0.0, a, w, z0, zs, k, 2 + tier, frame=trim, arch=True, band=0.28, sill=False, dark=True)
                F.balustrade("+t", 0.25, a - w / 2, a + w / 2, z0, trim, h=0.9, depth=0.25)
            else:
                F.window("+t", 0.0, a, w, z0, zs, k, 2 + tier, frame=trim, arch=True, band=0.28)
            n_win += 1
        F.balustrade("+t", 0.0, a - bw / 2 + 0.3, a + bw / 2 - 0.3, Z + 11.2, trim, h=0.75, depth=0.25)
        F.window("+t", 0.0, a, 1.0, Z + 17.0, Z + 18.1, k, 4, frame=trim)
    for i_, z_ in enumerate((Z + 7.8, Z + 12.2, Z + 17.0)):
        F.window("+t", 0.0, (TL[0] + TL[1]) / 2, 1.05, z_, z_ + (2.6 if i_ < 2 else 1.1), 0, 2 + i_, frame=trim)
    F.slab("+t", 0.0, LG[0], TL[1] + 0.3, Z + 18.5, Z + 19.4, 0.4, trim)
    F.slab("+t", 0.0, LG[0], TL[1], Z + 11.0, Z + 11.25, 0.2, trim)
    # The main gate: white marble, rusticated, a round arch, the arms on top
    # under a broken pediment; red-roofed sentry boxes either side.
    gc = (GB[0] + GB[1]) / 2
    F.box(gc - 2.9, gc + 2.9, -0.2, 0.55, Z, Z + 7.0, marble)
    for zb_ in np.arange(Z + 0.6, Z + 6.5, 0.62):
        F.box(gc - 2.95, gc - 2.05, 0.0, 0.62, zb_, zb_ + 0.48, marble * 0.97, top=False)
        F.box(gc + 2.05, gc + 2.95, 0.0, 0.62, zb_, zb_ + 0.48, marble * 0.97, top=False)
    F.window("+t", 0.56, gc, 3.0, Z, Z + 3.9, 0, 0, frame=marble, arch=True, band=0.42, sill=False, dark=True,
             depth=0.16)
    F.box(gc - 3.1, gc + 3.1, -0.2, 0.75, Z + 7.0, Z + 7.45, marble)                     # entablature
    F.box(gc - 1.35, gc + 1.35, 0.1, 0.7, Z + 7.45, Z + 9.5, marble)                      # the arms' cartouche
    F.box(gc - 0.75, gc + 0.75, 0.2, 0.82, Z + 9.5, Z + 10.3, marble)                     # the crown
    for sg in (-1, 1):
        F.box(gc + sg * 1.6, gc + sg * 3.1, -0.1, 0.72, Z + 7.45, Z + 7.95, marble)       # broken pediment ends
        F.box(gc + sg * 1.45, gc + sg * 1.9, -0.1, 0.72, Z + 7.95, Z + 8.5, marble)
    # The arms: a red and white lozengy shield under a gilt crown.
    red_, white_ = np.array(hexrgb("#c42a35")), np.array(hexrgb("#f7f3ee"))
    for i in range(4):
        for j in range(3):
            F.face_quad("+t", 0.7, gc - 0.6 + j * 0.4, gc - 0.2 + j * 0.4, Z + 7.85 + i * 0.3, Z + 8.15 + i * 0.3,
                        red_ if (i + j) % 2 else white_, out=0.02)
    F.box(gc - 0.45, gc + 0.45, 0.7, 0.86, Z + 9.1, Z + 9.45, np.array(hexrgb("#c9a548")))
    for d in (-0.35, 0.0, 0.35):
        F.box(gc + d - 0.08, gc + d + 0.08, 0.7, 0.86, Z + 9.45, Z + 9.75, np.array(hexrgb("#c9a548")))
    for sg in (-1, 1):
        sc = gc + sg * 5.0
        F.box(sc - 0.65, sc + 0.65, 1.0, 2.3, Z, Z + 2.35, np.array(hexrgb("#f4f1ea")))
        F.face_quad("+t", 2.3, sc - 0.42, sc + 0.42, Z + 0.3, Z + 2.0, dark, out=0.01)
        for z_, w_ in ((Z + 2.35, 0.8), (Z + 2.6, 0.55), (Z + 2.85, 0.3)):
            F.box(sc - w_, sc + w_, 1.65 - w_ * 0.9, 1.65 + w_ * 0.9, z_, z_ + 0.25, np.array(hexrgb("#c0372f")))
    stats["palace_gate"] = round(gc, 1)

    # -- South: the set-back block, its tower, the terrace wall -------------
    wing(-21.0, 0.0, -21.0, -9.6, 14.6, ("+t",), rows=[Z + 10.9], arch_top=True)
    F.box(-52.0, 0.0, -1.6, 0.0, gmin(-52, 0, -1.6, 0), Z + PL, wall)                     # terrace wall
    F.slab("+t", 0.0, -52.0, 0.0, Z + PL - 0.35, Z + PL, 0.25, trim)
    F.balustrade("+t", 0.0, -52.0, 0.0, Z + PL, trim, h=0.95)
    F.box(-21.0, 0.0, -9.6, -1.6, Z + PL - 0.3, Z + PL, np.array(hexrgb("#c9b9a0")))     # the terrace's paving
    for k in range(11):
        a = -50.0 + k * 4.6
        F.window("+t", 0.0, a + 2.3, 0.9, Z + 2.6, Z + 3.8, k, 0, frame=trim, pediment=True)
    # Ramparts' arched door at the south end (photo: an arch with an awning).
    F.window("+t", 0.0, -44.0, 2.6, Z, Z + 2.6, 0, 0, frame=trim, arch=True, dark=True, sill=False)
    # The south tower (cream, crenellated, above the set-back block).
    F.box(-23.5, -15.0, -15.5, -7.0, gmin(-23.5, -15, -15.5, -7), Z + 20.2, wall)
    F.battlements(-23.5, -15.0, -15.5, -7.0, Z + 20.2, 1.6, wall, merlon=1.05, gap=0.75)
    F.slab("+t", -7.0, -23.5, -15.0, Z + 19.6, Z + 20.2, 0.15, trim)
    for z_ in (Z + 15.8,):
        F.window("+t", -7.0, -19.25, 0.9, z_, z_ + 1.6, 0, 5, frame=trim)
    # The tower behind the gate block (seen over its roof, crenellated).
    F.box(21.0, 28.5, -19.5, -12.0, Z + 16.0, Z + 22.6, wall)
    F.battlements(21.0, 28.5, -19.5, -12.0, Z + 22.6, 1.6, wall, merlon=1.05, gap=0.75)
    F.window("+t", -12.0, 24.75, 0.9, Z + 18.6, Z + 20.2, 0, 5, frame=trim)

    # -- North: Tour Sainte-Marie, the grey curtain wall, the clock tower ---
    zg = gmin(48, 66, -16, 9)
    st = (48.0, 58.0, -15.0, -3.0)
    F.box(*st, zg, Z + 23.2, grey, style=RUBBLE, uvm=0.55)
    F.battlements(*st, Z + 23.2, 2.0, grey, merlon=1.1, gap=0.65, corbel=0.45)
    for zb_ in (Z + 7.0, Z + 15.0):
        F.slab("+t", st[3], st[0] - 0.05, st[1] + 0.05, zb_, zb_ + 0.35, 0.12, grey * 1.05)
    F.window("+t", st[3], 53.0, 0.9, Z + 17.2, Z + 19.0, 0, 5, frame=grey * 1.06, arch=True)
    F.window("+t", st[3], 53.0, 1.1, Z + 9.0, Z + 11.0, 0, 3, frame=grey * 1.06, arch=True)
    # Flag mast on top.
    p = F.xy(53.0, -9.0)
    # The sovereign's standard flies from Tour Sainte-Marie when the Prince
    # is in: a big red-over-white flag (4:5), drawn on both faces with a
    # gentle wave so it reads from across the port.
    _cyl(m, p[0], p[1], Z + 23.2, Z + 33.4, 0.16, np.array(hexrgb("#e9e9e4")), sides=8)
    _ball(m, p[0], p[1], Z + 33.55, 0.22, np.array(hexrgb("#c9a548")), None, 8, 5)
    fl_red, fl_white = np.array(hexrgb("#d3202e"), float), np.array(hexrgb("#f3f0ea"), float)
    FL, FH, ftop = 5.0, 4.0, Z + 33.1
    nst = 6
    for k in range(nst):
        t0_, t1_ = -9.0 + 0.15 + FL * k / nst, -9.0 + 0.15 + FL * (k + 1) / nst
        w0 = 0.35 * math.sin(math.pi * 1.5 * k / nst)
        w1 = 0.35 * math.sin(math.pi * 1.5 * (k + 1) / nst)
        for col_, z0_, z1_ in ((fl_red, ftop - FH / 2, ftop), (fl_white, ftop - FH, ftop - FH / 2)):
            for sg_ in (-1, 1):
                F.quad((53.0 + w0, t0_, z0_), (53.0 + w1, t1_, z0_), (53.0 + w1, t1_, z1_), (53.0 + w0, t0_, z1_),
                       col_, (sg_, 0, 0))
    stats["palace_flag_m"] = (FL, FH)
    # Curtain wall forward of the tower, to the clock tower.
    cw_ = (48.0, 66.0, -3.0, 3.5)
    F.box(*cw_, zg, Z + 13.5, grey, style=RUBBLE, uvm=0.55)
    F.battlements(*cw_, Z + 13.5, 1.6, grey, merlon=1.0, gap=0.7, corbel=0.35)
    for k, a in enumerate((50.5, 54.5)):
        F.window("+t", cw_[3], a, 1.0, Z + 7.0, Z + 8.8, k, 2, frame=grey * 1.06, arch=True)
    F.window("+t", cw_[3], 59.5, 1.4, Z + 0.0, Z + 2.6, 0, 0, frame=grey * 1.06, arch=True, dark=True, sill=False)
    ct = (62.5, 68.5, 3.5, 9.5)
    F.box(*ct, zg, Z + 22.0, grey, style=RUBBLE, uvm=0.55)
    F.battlements(*ct, Z + 22.0, 1.7, grey, merlon=0.95, gap=0.6, corbel=0.4)
    # The clock: a white dial with a dark ring and hands, facing the square.
    cx_, cz_ = 65.5, Z + 18.4
    th = np.linspace(0, 2 * math.pi, 17)
    for r_, col_, out_ in ((0.95, np.array(hexrgb("#2b2b2b")), 0.04), (0.82, np.array(hexrgb("#f5f2ea")), 0.06)):
        P = [F.E(cx_ + r_ * math.cos(a), ct[3] + out_, cz_ + r_ * math.sin(a)) for a in th]
        ctr = F.E(cx_, ct[3] + out_, cz_)
        for i in range(16):
            A_, B_ = P[i], P[i + 1]
            if (np.cross(A_ - ctr, B_ - ctr) * F.want(cx_, ct[3], cz_, 0, 1, 0)).sum() < 0:
                A_, B_ = B_, A_
            m.tri(ctr, A_, B_, col_, PLAIN)
    F.box(cx_ - 0.04, cx_ + 0.04, ct[3] + 0.06, ct[3] + 0.09, cz_, cz_ + 0.6, np.array(hexrgb("#1c1c1c")), top=False)
    F.box(cx_, cx_ + 0.45, ct[3] + 0.06, ct[3] + 0.09, cz_ - 0.04, cz_ + 0.04, np.array(hexrgb("#1c1c1c")), top=False)
    F.window("+t", ct[3], 65.5, 0.7, Z + 12.0, Z + 13.6, 0, 4, frame=grey * 1.06, arch=True)
    F.window("+s", ct[1], 6.5, 0.7, Z + 12.0, Z + 13.6, 0, 4, frame=grey * 1.06, arch=True)
    # Low grey wing behind (ramparts side): a terrace with battlements.
    wing(58.0, 80.0, -30.0, -3.0, 8.0, ("+s",), rows=[Z + 3.0])

    # -- The wings round the Cour d'honneur and behind ---------------------
    wing(-27.0, 0.0, -63.0, -21.0, 17.0, ("-s",))
    wing(-50.0, -27.0, -21.0, -1.6, 15.5, ("-s",))
    wing(-48.0, -33.0, -90.0, -21.0, 14.0, ("-s", "-t"))
    wing(-33.0, 25.0, -92.0, -63.0, 17.0, ("-t", "+t"))
    wing(23.0, 37.0, -63.0, -21.0, 17.5, ("-s", "+s"))
    wing(37.0, 58.0, -62.0, -15.0, 8.0, ("+s", "-t"), rows=[Z + 3.4], roof_=False)
    F.balustrade("+s", 58.0, -62.0, -15.0, Z + 8.0, trim, h=0.9)
    wing(25.0, 45.0, -92.0, -62.0, 11.0, ("+s", "-t"))
    # The front block's roof, the court face of the front wing.
    F.hip_roof(GB[0], GB[1], -21.0, 0.0, Z + 16.4, roof)
    F.hip_roof(LG[0], TL[1], -21.0, 0.0, Z + 19.4, roof)
    # Chimneys along the front roofs (photo).
    for a in (3.0, 11.0, 19.5, 30.0, 40.0):
        zt_ = Z + (19.2 if a < 27 else 22.0)
        F.box(a - 0.32, a + 0.32, -3.0, -2.4, Z + 16.0, zt_, wall)
        F.box(a - 0.4, a + 0.4, -3.08, -2.32, zt_, zt_ + 0.2, trim)
    # The back tower (LiDAR: a high point in the back wing).
    F.box(-8.5, -0.5, -86.0, -78.0, Z + 17.0, Z + 23.0, wall)
    F.battlements(-8.5, -0.5, -86.0, -78.0, Z + 23.0, 1.5, wall, merlon=1.0, gap=0.7)

    # -- Cour d'honneur -----------------------------------------------------
    CS, CT = (0.0, 23.0), (-63.0, -21.0)
    ss_, tt_ = np.meshgrid(np.linspace(CS[0], CS[1], 24), np.linspace(CT[0], CT[1], 40))
    pp_ = F.O[None] + ss_.ravel()[:, None] * F.u[None] + tt_.ravel()[:, None] * F.v[None]
    zin = mntR.sample(pp_[:, 0], pp_[:, 1])
    # The court's floor (LiDAR 64 m), over the terrain model's guess there.
    zc = max(Z + 2.0, float(np.nanmax(zin)) + 0.12 if np.isfinite(zin).any() else Z + 2.0)
    F.box(CS[0], CS[1], CT[0], CT[1], zc - 0.4, zc, np.array(hexrgb("#d6cebd")), sides="")
    # Pebble mosaic: a grid of darker bands and a star in the middle.
    pc = np.array(hexrgb("#9f968a"), float)
    for a in np.linspace(CS[0] + 2.3, CS[1] - 2.3, 5):
        F.box(a - 0.18, a + 0.18, CT[0] + 2, CT[1] - 2, zc, zc + 0.02, pc, sides="")
    for b in np.linspace(CT[0] + 3, CT[1] - 3, 8):
        F.box(CS[0] + 2, CS[1] - 2, b - 0.18, b + 0.18, zc, zc + 0.02, pc, sides="")
    # Court faces of the wings round it: frescoed ochre, rows of windows.
    # The gate comes in through the east wing (-t at t -21); the north wing
    # (-s at s 23) and the west end (+t at t -63) are plain ochre fronts.
    for face, c, a0, a1 in (("-s", 23.0, -63.0, -21.0), ("-t", -21.0, 0.0, 23.0), ("+t", -63.0, 0.0, 23.0)):
        F.face_quad(face, c, a0, a1, zc, Z + 16.5, court_wall, out=0.02)
        nb = int(round((a1 - a0) / 3.8))
        for k in range(nb):
            a = a0 + (k + 0.5) * (a1 - a0) / nb
            if face == "-t" and abs(a - 11.0) < 2.0:
                F.window(face, c, a, 3.0, zc, zc + 2.8, 0, 0, frame=marble, arch=True, dark=True, sill=False)
                continue
            for i, z_ in enumerate((zc + 1.2, zc + 6.2, zc + 10.6)):
                F.window(face, c, a, 1.2, z_, z_ + 2.2, k, i, frame=trim, arch=(i == 1))
                n_win += 1
            # Painted panels between the windows (the frescoes).
            F.face_quad(face, c, a + 0.95, a + 1.7, zc + 5.4, zc + 9.4, np.array(hexrgb("#c99d68")), out=0.04)
    # The south side of the court: the Galerie d'Hercule, two tiers of open
    # arcades along the whole south wing (it serves the south wing's rooms),
    # and in the middle the double-revolution staircase in Carrara marble
    # coming down into the court. A frame of its own: s along the gallery
    # from the east end (t -21) to the west (t -63), t out into the court.
    G = Frame(m, F.xy(0.0, -21.0), -F.v)
    GL = 42.0
    G.face_quad("+t", 0.0, 0.0, GL, zc, Z + 17.0, court_wall, out=0.02)
    nbay = 9
    bw_ = GL / nbay
    for k in range(nbay):
        a = (k + 0.5) * bw_
        G.window("+t", 0.0, a, 3.0, zc, zc + 3.2, k, 0, frame=trim, arch=True, dark=True, sill=False)
        G.window("+t", 0.0, a, 3.0, zc + 6.0, zc + 9.2, k, 1, frame=trim, arch=True, dark=True, sill=False)
        G.balustrade("+t", 0.3, a - 1.5, a + 1.5, zc + 6.0, trim, h=0.9, depth=0.25)
        # Frescoed panels over the upper arches (Hercules' labours).
        G.face_quad("+t", 0.0, a - 1.6, a + 1.6, zc + 9.9, zc + 11.6, np.array(hexrgb("#c99d68")), out=0.04)
        if k < nbay - 1:
            G.slab("+t", 0.0, a + bw_ / 2 - 0.45, a + bw_ / 2 + 0.45, zc, Z + 15.5, 0.2, trim)
    G.slab("+t", 0.0, 0.0, GL, zc + 5.5, zc + 6.0, 0.6, trim)
    G.slab("+t", 0.0, 0.0, GL, zc + 12.0, zc + 12.4, 0.35, trim)
    # Staircase: a landing on the middle arch, two flights curving out and
    # down, their feet turned into the court (as at Fontainebleau), black
    # Portor balls along the marble rail.
    cx, land_t, top = GL / 2, 3.2, zc + 6.0
    G.box(cx - 2.2, cx + 2.2, 0.0, land_t, zc, top, marble)
    G.balustrade("+t", land_t, cx - 2.2, cx + 2.2, top, marble, h=0.9, depth=0.25)
    portor = np.array(hexrgb("#1d1b1a"), float)
    for sg in (-1, 1):
        n_ = 28
        R0 = 6.4
        for k in range(n_):
            th = math.pi / 2 * (k + 0.5) / n_
            a = cx + sg * (2.2 + R0 * math.sin(th))
            b = land_t + R0 * (1 - math.cos(th))
            tan = sg * math.cos(th) * G.u + math.sin(th) * G.v
            p = G.xy(a, b)
            zt = top - (top - zc) * (k + 1) / n_ + 0.18
            _box(m, p[0], p[1], tan, R0 * math.pi / 2 / n_ + 0.08, 1.7, zc, zt, marble * (0.97 + 0.03 * (k % 2)))
            # The outer rail.
            if k % 2 == 0:
                ro = G.xy(cx + sg * (2.2 + (R0 + 0.8) * math.sin(th)), land_t + (R0 + 0.8) * (1 - math.cos(th)))
                _box(m, ro[0], ro[1], tan, R0 * math.pi / n_ + 0.1, 0.22, zt, zt + 0.9, marble)
                if k % 6 == 0:
                    _ball(m, ro[0], ro[1], zt + 1.05, 0.16, portor, None, 6, 4)
        stats["palace_stair_steps"] = 2 * n_
    stats["palace"] = {"square": round(Z, 2), "court": round(zc, 2), "windows": n_win}

    # -- On the square: Louis XIV's cannons, chains ---------------------------
    # Seven bronze cannons on wooden carriages along the front, four south
    # of the gate and three north of it, each with its pyramid of balls
    # (OpenStreetMap's historic=cannon, pointing out across the square).
    bronze = np.array(hexrgb("#6b5532"), float)
    wood = np.array(hexrgb("#3d2f25"), float)
    for a in (-8.2, -4.7, -1.1, 1.7, 21.6, 25.1, 28.6):
        F.box(a - 0.45, a + 0.45, 2.0, 3.8, Z, Z + 0.75, wood)
        for j in range(6):
            r_ = 0.24 - 0.02 * j
            F.box(a - r_, a + r_, 1.9 + j * 0.45, 2.35 + j * 0.45, Z + 0.85 - r_, Z + 0.85 + r_, bronze)
        q = F.xy(a + 1.4, 2.9)
        for lv, nb in enumerate((3, 2, 1)):
            for i in range(nb):
                for j in range(nb):
                    off = (i - (nb - 1) / 2) * 0.24
                    off2 = (j - (nb - 1) / 2) * 0.24
                    pp = q + F.u * off + F.v * off2
                    _ball(m, pp[0], pp[1], Z + 0.12 + lv * 0.19, 0.12, np.array(hexrgb("#2e2e2c")), None, 6, 4)
    # Low green posts and chains round the coaches' bay (OSM barrier=chain).
    bol = np.array(hexrgb("#33463c"), float)
    for line in (((57.8, 47.1), (35.5, 59.6), (26.0, 33.2)), ((21.6, 35.3), (30.8, 61.7), (23.7, 64.1))):
        for (s0, t0), (s1, t1) in zip(line[:-1], line[1:]):
            L = math.hypot(s1 - s0, t1 - t0)
            n_p = max(1, int(round(L / 2.5)))
            prev = None
            for k in range(n_p + 1):
                f = k / n_p
                ps, pt = s0 + (s1 - s0) * f, t0 + (t1 - t0) * f
                p = F.xy(ps, pt)
                z0 = mntR.sample(np.array([p[0]]), np.array([p[1]]))[0]
                z0 = Z if not np.isfinite(z0) else float(z0)
                _cyl(m, p[0], p[1], z0, z0 + 0.7, 0.07, bol, sides=8)
                if prev is not None:
                    q0, zq = prev
                    mid, zc_ = (q0 + p) / 2, (zq + z0) / 2 + 0.45
                    _box(m, mid[0], mid[1], p - q0, float(np.linalg.norm(p - q0)), 0.04, zc_ - 0.025, zc_ + 0.025, bol * 0.6)
                prev = (p, z0)
    stats["palace_cannons"] = 7
    return F


# ------------------------------------------------- the Oceanographic Museum ----
MUSEE = "BATIMENT0000000074078955"
PRISON = "BATIMENT0000000324292895"        # Maison d'arrêt (prison())
MUSEE_O = (1056417.9, 6302352.8)          # the sea face's south-west corner (BD TOPO)
MUSEE_U = (74.3, 68.7)                    # along the sea face, south-west to north-east
MUSEE_STONE = "#eae6dc"                   # pale La Turbie limestone (photos)
MUSEE_L = 101.2


def musee_oceanographique(m, gm, mntR, stats):
    """The Oceanographic Museum (1910), in a frame on its sea face: s from
    the south-west end (0) to the north-east (101 m), t out to sea (the
    street face is at t = -19, the entrance pavilion reaches t = -33),
    z m NGF. Street side from the 2015 and 2019 Mapillary photos; the sea
    face (no street photos) after the same order: rusticated base in the
    cliff, tall arches between paired columns, the frieze, a balustrade."""
    F = Frame(m, MUSEE_O, MUSEE_U)
    L = MUSEE_L
    zs_ = mntR.sample(np.array([F.xy(50.5, -38.0)[0]]), np.array([F.xy(50.5, -38.0)[1]]))[0]
    Z = float(zs_) if np.isfinite(zs_) else 54.0          # Avenue Saint-Martin, in front of the steps
    st = np.array(hexrgb(MUSEE_STONE), float)
    lit = st * 1.03
    shade = st * 0.93
    dark = np.array(hexrgb("#2a2724"), float)
    TOP = Z + 23.6                                         # cornice (LiDAR roof 78-79)
    BODY = (-19.0, 0.0)
    CP = (36.2, 65.3)                                      # the central pavilion
    n_win = 0

    def ground(s, t):
        g = _ground_min(F, mntR, s, s, t, t, n=2)
        return Z if g is None else g

    # The body, down to the rock on the sea side.
    zrock = min(Z - 1, _ground_min(F, mntR, 0, L, 0.5, 3.0) or Z) - 0.5
    F.box(0.0, L, BODY[0], BODY[1], zrock, TOP, st, top=True)
    # -- Sea face ------------------------------------------------------------
    # Rusticated base: the rock-cut storeys, in big coursed blocks.
    F.face_quad("+t", 0.0, 0.0, L, zrock, Z - 3.0, st * 0.96, out=0.05, style=RUBBLE, uvm=0.45)
    for zb_ in np.arange(Z - 3.0, Z + 3.0, 0.9):
        F.slab("+t", 0.0, 0.0, L, zb_, zb_ + 0.55, 0.12, lit)                             # banded storey
    nb = 13
    for k in range(nb):
        a = (k + 0.5) * L / nb
        g = ground(a, 2.0)
        for i, z_ in enumerate(np.arange(Z - 9.0, g + 3.0, -6.0)):
            F.window("+t", 0.0, a, 1.6, z_, z_ + 2.4, k, i, frame=lit, arch=True, band=0.3)
            n_win += 1
        F.window("+t", 0.0, a, 1.9, Z - 1.6, Z + 1.4, k, 5, frame=lit, arch=True, band=0.3)
    # Buttress strips: the paired columns' rhythm carried down the base to
    # the rock, a little proud of the rustication.
    for k in range(nb + 1):
        a = k * L / nb
        if CP[0] - 0.5 < a < CP[1] + 0.5:
            continue
        g = min(ground(a, 1.0), Z - 3.0)
        F.box(a - 0.95, a + 0.95, 0.0, 0.4, g - 1.0, Z + 3.6, st * 0.98, sides="sST", style=RUBBLE, uvm=0.45)
    # Main order: tall arched windows between paired columns.
    zc0, zc1 = Z + 4.2, Z + 18.4
    F.slab("+t", 0.0, -0.2, L + 0.2, Z + 3.6, Z + 4.2, 0.55, lit)                        # string course
    bays = [(k + 0.5) * L / nb for k in range(nb)]
    for k, a in enumerate(bays):
        if CP[0] < a < CP[1]:
            continue
        F.balustrade("+t", 0.0, a - 1.6, a + 1.6, Z + 4.2, lit, h=0.95, depth=0.35)
        F.window("+t", 0.0, a, 2.6, Z + 5.2, Z + 14.0, k, 6, frame=lit, arch=True, band=0.32, sill=False)
        n_win += 1
    for k in range(nb + 1):
        a = k * L / nb
        if CP[0] - 0.5 < a < CP[1] + 0.5:
            continue
        for d in (-0.55, 0.55):
            F.column(a + d, 0.55, zc0, zc1, 0.36, lit)
    # Frieze (the research ships' names), cornice, balustrade.
    F.slab("+t", 0.0, -0.3, L + 0.3, zc1, zc1 + 0.6, 0.75, lit)
    F.slab("+t", 0.0, -0.3, L + 0.3, zc1 + 0.6, zc1 + 2.4, 0.55, st)
    rnd = rng_for(MUSEE)
    for k, a in enumerate(bays):
        if CP[0] < a < CP[1]:
            continue
        for j in range(int(rnd.integers(6, 10))):
            x = a - 2.2 + j * 0.5
            F.slab("+t", 0.55, x, x + 0.32, zc1 + 1.15, zc1 + 1.75, 0.03, np.array(hexrgb("#b9b2a2")))
    F.slab("+t", 0.0, -0.5, L + 0.5, zc1 + 2.4, TOP, 1.1, lit)                         # cornice
    F.balustrade("+t", 1.0, 0.0, L, TOP, lit, h=1.1)
    # Central pavilion on the sea: three great arches, a higher attic.
    F.box(CP[0], CP[1], 0.0, 1.8, zrock, Z + 28.5, st, top=True)
    F.face_quad("+t", 1.8, CP[0], CP[1], zrock, Z - 3.0, st * 0.96, out=0.02, style=RUBBLE, uvm=0.45)
    w3 = (CP[1] - CP[0]) / 3
    for k in range(3):
        a = CP[0] + (k + 0.5) * w3
        F.window("+t", 1.8, a, 5.2, Z + 4.6, Z + 15.0, k, 6, frame=lit, arch=True, band=0.45)
        F.balustrade("+t", 1.8, a - 2.6, a + 2.6, Z + 4.0, lit, h=1.0, depth=0.4)
        F.window("+t", 1.8, a, 2.2, Z - 1.6, Z + 1.6, k, 5, frame=lit, arch=True, band=0.3)
        n_win += 2
    for a in (CP[0] + 0.6, CP[0] + w3, CP[0] + 2 * w3, CP[1] - 0.6):
        for d in (-0.6, 0.6):
            F.column(a + d, 2.35, Z + 4.2, Z + 20.6, 0.45, lit)
    F.slab("+t", 1.8, CP[0] - 0.3, CP[1] + 0.3, Z + 20.6, Z + 23.8, 0.7, lit)
    F.slab("+t", 1.8, CP[0] - 0.5, CP[1] + 0.5, Z + 23.8, Z + 24.8, 1.0, lit)
    F.balustrade("+t", 2.8, CP[0], CP[1], Z + 28.5, lit, h=1.1)

    # -- Street face ---------------------------------------------------------
    sf = BODY[0]
    for zb_ in np.arange(Z + 0.4, Z + 6.6, 0.95):
        F.slab("-t", sf, 0.0, L, zb_, zb_ + 0.55, 0.14, lit)                             # banded base
    F.slab("-t", sf, -0.3, L + 0.3, Z + 6.6, Z + 7.3, 0.4, lit)
    nbs = 5
    for side, (a0, a1) in (("w", (6.5, CP[0])), ("e", (CP[1], L - 6.5))):
        for k in range(nbs):
            a = a0 + (k + 0.5) * (a1 - a0) / nbs
            F.window("-t", sf, a, 1.4, Z + 2.0, Z + 4.2, k, 1, frame=lit)                 # basement grilles
            F.window("-t", sf, a, 2.6, Z + 8.0, Z + 15.0, k, 2, frame=lit, arch=True, band=0.32)
            F.balustrade("-t", sf, a - 1.5, a + 1.5, Z + 7.3, lit, h=0.85, depth=0.3)
            n_win += 2
        for k in range(nbs + 1):
            a = a0 + k * (a1 - a0) / nbs
            for d in (-0.5, 0.5):
                F.column(a + d, sf - 0.55, Z + 7.3, Z + 19.0, 0.34, lit)
    F.slab("-t", sf, -0.3, L + 0.3, Z + 19.0, Z + 21.6, 0.6, st)
    for a0, a1 in ((7.0, CP[0] - 1.0), (CP[1] + 1.0, L - 7.0)):
        for j in range(int((a1 - a0) / 0.55)):
            x = a0 + j * 0.55
            if rnd.random() < 0.8:
                F.slab("-t", sf - 0.6, x, x + 0.34, Z + 19.9, Z + 20.6, 0.03, np.array(hexrgb("#b9b2a2")))
    F.slab("-t", sf, -0.5, L + 0.5, Z + 21.6, TOP, 1.0, lit)
    F.balustrade("-t", sf - 1.0, 0.0, L, TOP, lit, h=1.1)
    # Corner pavilions with small domes and ball finials (NE corner, photo
    # 2019; the SW one alike).
    for a0, a1 in ((0.0, 6.5), (L - 6.5, L)):
        F.box(a0, a1, sf - 1.2, sf + 1.0, Z, TOP + 1.2, st)
        for zb_ in np.arange(Z + 0.4, Z + 6.6, 0.95):
            F.slab("-t", sf - 1.2, a0, a1, zb_, zb_ + 0.55, 0.14, lit)
        F.window("-t", sf - 1.2, (a0 + a1) / 2, 2.0, Z + 9.0, Z + 15.0, 0, 2, frame=lit, arch=True, band=0.35)
        F.balustrade("-t", sf - 1.2, a0 + 0.4, a1 - 0.4, Z + 7.3, lit, h=0.85)
        F.slab("-t", sf - 1.2, a0 - 0.3, a1 + 0.3, TOP + 1.2, TOP + 1.9, 0.6, lit)
        c = F.xy((a0 + a1) / 2, sf - 0.1)
        dome_cap(m, c, F.u, F.v, 2.6, TOP + 1.9, 3.4, st * 0.98, lit)
        _ball(m, c[0], c[1], TOP + 6.0, 0.55, lit, None, 10, 6)
        n_win += 1
    # Entrance pavilion: steps, the rusticated arch, the great window between
    # paired giant columns, sculpture groups, the inscription, the pediment.
    pf = -33.0
    ZT = Z + 27.4
    F.box(CP[0], CP[1], pf, sf, Z - 0.5, ZT, st)
    F.box(CP[0], CP[1], sf, 0.0, TOP, Z + 28.5, st)                                   # the hall's high roof
    for zb_ in np.arange(Z + 2.0, Z + 12.5, 1.05):
        F.slab("-t", pf, CP[0], CP[1], zb_, zb_ + 0.62, 0.25, lit)                       # carved bands
        F.slab("-s", CP[0], pf, sf, zb_, zb_ + 0.62, 0.25, lit)
        F.slab("+s", CP[1], pf, sf, zb_, zb_ + 0.62, 0.25, lit)
        F.slab("-t", pf - 0.25, CP[0], CP[1], zb_ + 0.2, zb_ + 0.42, 0.06, shade)
    mid = (CP[0] + CP[1]) / 2
    F.slab("-t", pf, mid - 4.6, mid + 4.6, Z + 1.5, Z + 12.6, 0.4, st)                   # the portal's frame
    F.window("-t", pf - 0.4, mid, 3.6, Z + 2.6, Z + 7.6, 0, 0, frame=lit, arch=True, band=0.55, sill=False,
             dark=True, depth=0.25)
    F.slab("-t", pf - 0.4, mid - 0.9, mid + 0.9, Z + 9.0, Z + 10.6, 0.35, lit)          # the arms over the arch
    for k in range(9):                                                                   # steps
        d = 0.42 * (9 - k)
        F.box(mid - 8.0 - d * 0.6, mid + 8.0 + d * 0.6, pf - d, pf, Z - 0.4, Z + 0.3 + k * 0.25, st * (0.98 + 0.01 * (k % 2)))
    F.slab("-t", pf, CP[0] - 0.3, CP[1] + 0.3, Z + 12.6, Z + 13.4, 0.6, lit)
    for face, c in (("-s", CP[0]), ("+s", CP[1])):
        F.slab(face, c, pf, sf, Z + 12.6, Z + 13.4, 0.6, lit)
        F.window(face, c, (pf + sf) / 2, 2.4, Z + 14.4, Z + 20.0, 0, 3, frame=lit, arch=True, band=0.4)
        for d in (pf + 1.2, sf - 1.2):
            F.column(c + (-0.7 if face[0] == "-" else 0.7), d, Z + 13.4, Z + 23.6, 0.42, lit)
        F.slab(face, c, pf, sf, Z + 23.6, ZT, 0.8, lit)
    F.balustrade("-t", pf - 0.6, mid - 4.5, mid + 4.5, Z + 13.4, lit, h=1.0)
    F.window("-t", pf, mid, 4.6, Z + 13.6, Z + 20.0, 0, 3, frame=lit, arch=True, band=0.5, sill=False)
    for sg in (-1, 1):
        for d in (5.2, 6.7):
            F.column(mid + sg * d, pf - 0.7, Z + 13.4, Z + 23.6, 0.48, lit)
        # Sculpture groups either side (rough masses of figures).
        for j in range(5):
            a = mid + sg * (9.0 + j * 0.7)
            F.box(a - 0.5, a + 0.5, pf - 1.2 + 0.1 * j, pf, Z + 15.5 + 0.2 * j, Z + 19.8 - 0.55 * j, lit * (0.96 + 0.02 * j))
        F.box(mid + sg * 9.0 - 2.2, mid + sg * 9.0 + 3.6, pf - 1.4, pf, Z + 13.4, Z + 15.5, lit)
    F.slab("-t", pf, CP[0] - 0.3, CP[1] + 0.3, Z + 23.6, Z + 24.4, 0.9, lit)             # entablature
    F.slab("-t", pf - 0.6, mid - 9.0, mid + 9.0, Z + 24.4, Z + 25.6, 0.3, st)            # the inscription band
    for j in range(22):
        x = mid - 7.6 + j * 0.7
        F.slab("-t", pf - 0.9, x, x + 0.42, Z + 24.7, Z + 25.3, 0.03, np.array(hexrgb("#9d9584")))
    F.slab("-t", pf - 0.6, CP[0], CP[1], Z + 25.6, ZT, 0.9, lit)                         # cornice
    # Pediment: a low triangle over the middle.
    for k in range(8):
        w_ = 8.5 * (1 - k / 8)
        F.box(mid - w_, mid + w_, pf - 1.2, pf + 2.0, ZT + k * 0.3, ZT + (k + 1) * 0.3, lit, top=(k == 7))
    F.balustrade("-t", pf - 0.2, CP[0], mid - 9.0, ZT, lit, h=1.1)
    F.balustrade("-t", pf - 0.2, mid + 9.0, CP[1], ZT, lit, h=1.1)
    # Roof terrace: pale paving with a skylight over the great hall.
    F.box(4.0, L - 4.0, BODY[0] + 3.0, -3.0, TOP, TOP + 0.08, np.array(hexrgb("#d8d1c3")), sides="")
    F.box(CP[0] + 4, CP[1] - 4, -15.0, -4.0, Z + 28.5, Z + 29.6, np.array(hexrgb("#9aa9ad")), style=ROOF_GLASS)
    # The yellow submarine on the pavement north-east of the entrance (2019).
    yel = np.array(hexrgb("#f1c21b"), float)
    sa, sb_, tt = 76.0, 81.5, -27.0
    nseg = 16
    for j in range(nseg):
        s0 = sa + j * (sb_ - sa) / nseg
        r_ = 0.95 * math.sin(math.pi * (j + 0.5) / nseg) ** 0.45
        p = F.xy(s0 + (sb_ - sa) / nseg / 2, tt)
        # Three overlapping boxes per slice: a rounded hull.
        _box(m, p[0], p[1], F.u, (sb_ - sa) / nseg + 0.02, 2 * r_, Z + 1.2 - r_ * 0.42, Z + 1.2 + r_ * 0.42, yel)
        _box(m, p[0], p[1], F.u, (sb_ - sa) / nseg + 0.02, 2 * r_ * 0.42, Z + 1.2 - r_, Z + 1.2 + r_, yel)
        _box(m, p[0], p[1], F.u, (sb_ - sa) / nseg + 0.02, 2 * r_ * 0.78, Z + 1.2 - r_ * 0.78, Z + 1.2 + r_ * 0.78, yel)
    for j, a in enumerate((77.2, 78.0, 78.8, 79.6, 80.4)):
        p = F.xy(a, tt - 0.97)
        _ball(m, p[0], p[1], Z + 1.35, 0.17, np.array(hexrgb("#3d4a52")), None, 8, 5)        # portholes
    F.box(78.0, 79.4, tt - 0.55, tt + 0.55, Z + 2.0, Z + 3.0, yel)
    F.box(77.0, 80.5, tt - 0.6, tt + 0.6, Z, Z + 0.35, np.array(hexrgb("#55534e")))
    stats["musee"] = {"street": round(Z, 2), "rock": round(zrock, 2), "windows": n_win}
    return F


# ------------------------------------------------------------ Place d'Armes ----
# The market square of La Condamine at the foot of the Rocher, rebuilt from
# the IGN 20 cm photo, the LiDAR, OpenStreetMap and street photos on
# Mapillary (2019, 2021, 2025): red herringbone brick laid in a grid of grey
# stone bands, the market's red-canopied stalls, tall pines and small trees,
# the brown snack-bar kiosk with its barrel roofs, the newspaper kiosk, the
# fenced wooden playground, cast-iron lamps with clusters of globes, brown
# bollards along the road. On the north side the salmon-pink block stands
# on a white stone arcade; on the east side the Halles du marché (cream,
# arched, a tiled roof with a glazed ridge) open onto the square through
# their own arcade. The Rampe Major, the paved stepped ramp up to the
# Palace, is laid in the same brick and grey stone.
ARMES_NAME = "Place d'Armes"
ARMES = None                  # {"poly", "zone", "o", "u", "v", "rampe"}
CONDA = None                  # {"zone", "park", "ped", "ped_poly"}
ARMES_BAND = 4.7              # m between the grey stone bands (the photo's period)
ARMES_PHASE = (0.0, -7.6)     # where a band crosses, m along / across the grid from the square's middle
ARMES_C = (1055944.0, 6302584.0)
HERRING = 11                  # GROUND style: herringbone brick in grey bands (viewer/filmset.js)
ARMES_NORTH = "BATIMENT0000000074074633"     # the arcaded block on the north side
ARMES_HALLE = "BATIMENT0000000074074614"     # Halles du marché de la Condamine
# Arcades on the square: height of the arch row, depth, bay.
ARCADES = {ARMES_NORTH: {"h": 5.2, "d": 3.2, "bay": 4.3, "pier": 1.0},
           ARMES_HALLE: {"h": 5.6, "d": 3.4, "bay": 4.6, "pier": 1.1}}
ARCADE_NOW = None             # the arcade edges of the building being built (add_walls)
ARMES_BRICK = "#a4553f"
ARMES_IRON = "#3a2a22"        # the lamps and bollards: dark brown cast iron
ARMES_KIOSK = "#4b3125"       # varnished dark wood
ARMES_KIOSK_ROOF = "#6e4430"  # the barrel roofs, brown
ARMES_CANOPY = "#c3312d"      # the market stalls' red canopies


# La Condamine, the district between the Rocher, the port and Sainte-Dévote
# (metres east / north of ARMES_C): its streets get the pavements, kerbs
# and street trees the circuit has, and its walls their photo colours.
CONDAMINE = [(-130, -40), (60, -40), (250, 40), (250, 520), (-130, 520)]


def condamine_zone():
    return Polygon([(ARMES_C[0] + e, ARMES_C[1] + n) for e, n in CONDAMINE])


def armes_setup(ways):
    sq = None
    for t, pts, closed in ways:
        if closed and t.get("name") == ARMES_NAME and t.get("highway") == "pedestrian" and len(pts) > 3:
            sq = Polygon(pts).buffer(0)
    if sq is None or sq.is_empty:
        return None
    # The grid runs along the north frontage (the square's long side).
    rr = np.asarray(sq.minimum_rotated_rectangle.exterior.coords)
    ed = [rr[i + 1] - rr[i] for i in range(2)]
    u = max(ed, key=lambda e: np.linalg.norm(e))
    u = u / np.linalg.norm(u)
    if u[0] < 0:
        u = -u
    v = np.array([-u[1], u[0]])
    rampe = [LineString(p) for t, p, c in ways if t.get("name") == "Rampe Major" and t.get("highway") == "steps"]
    rampe = [r for r in rampe if r.intersects(box(X0, Y0, X1, Y1)) and r.distance(sq) < 30]
    return {"poly": sq, "zone": sq.buffer(12.0), "o": np.array(ARMES_C), "u": u, "v": v, "rampe": rampe}


def armes_grid(x, y):
    """Metres along / across the square's grid (TEXCOORD_1 of its paving)."""
    d = np.column_stack([x, y]) - ARMES["o"]
    return np.column_stack([d @ ARMES["u"] - ARMES_PHASE[0], d @ ARMES["v"] - ARMES_PHASE[1]])


def _arcade_mask(a, b):
    """Which wall edges a-b run along the arcade of the building being built."""
    if ARCADE_NOW is None or not len(a):
        return np.zeros(len(a), bool)
    ok = np.zeros(len(a), bool)
    for L in ARCADE_NOW["lines"]:
        da = shapely.distance(shapely.points(a), L)
        db = shapely.distance(shapely.points(b), L)
        ok |= (da < 0.6) & (db < 0.6)
    return ok


def _arch_face(m, a, b, zf, ztop, nb, pier, col, out):
    """A wall from a to b (its outside towards `out`), from the floor zf
    (one per bay) up to ztop, pierced by nb round arches: the arcade's
    face, both sides."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    L = float(np.linalg.norm(b - a))
    u = (b - a) / L
    w = L / nb
    for i in range(nb):
        z0 = float(zf[i])
        ow = w - pier
        r = ow / 2
        spring = max(z0 + 2.4, ztop - 0.9 - r)
        s0, s1 = i * w, (i + 1) * w
        c = (s0 + s1) / 2
        arc = [(c + r * math.cos(t), spring + r * math.sin(t)) for t in np.linspace(0, math.pi, 13)]
        hole = [(c + r, z0)] + arc + [(c - r, z0)]
        outer = [(s0, z0 - 0.4), (s1, z0 - 0.4), (s1, ztop), (s0, ztop)]
        face = Polygon(outer).difference(Polygon(hole).buffer(0))
        for q in to_polys(face):
            t_ = triangulate(orient(q, 1.0))
            if t_ is None:
                continue
            v, tri = t_
            xy = a[None] + u[None] * v[:, :1]
            for sg, off in ((1.0, 0.02), (-1.0, -0.3)):
                P = enu_fast(xy[:, 0] + out[0] * off, xy[:, 1] + out[1] * off, v[:, 1])
                A, B_, C = P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]]
                nn = np.cross(B_ - A, C - A)
                wv = enu_fast(np.array([xy[0, 0], xy[0, 0] + out[0] * sg]), np.array([xy[0, 1], xy[0, 1] + out[1] * sg]),
                              np.array([z0, z0]))
                want = np.tile(wv[1] - wv[0], (len(tri), 1))
                flip = (nn * want).sum(1) < 0
                B2 = np.where(flip[:, None], C, B_)
                C2 = np.where(flip[:, None], B_, C)
                m.tri(A, B2, C2, col * (1.0 if sg > 0 else 0.8), PLAIN)
        # The arch's intrados and the pier's sides: the face's depth.
        for t0, t1 in zip(np.linspace(0, math.pi, 13)[:-1], np.linspace(0, math.pi, 13)[1:]):
            p0 = np.array([c + r * math.cos(t0), spring + r * math.sin(t0)])
            p1 = np.array([c + r * math.cos(t1), spring + r * math.sin(t1)])
            q0, q1 = a + u * p0[0], a + u * p1[0]
            E = lambda q, z, o: enu_fast(np.array([q[0] + out[0] * o]), np.array([q[1] + out[1] * o]), np.array([z]))[0]
            _quad(m, E(q0, p0[1], 0.02)[None], E(q1, p1[1], 0.02)[None], E(q1, p1[1], -0.3)[None],
                  E(q0, p0[1], -0.3)[None], col * 0.9, np.array([[0.0, 0.0, -1.0]]))
        for sx in (c - r, c + r):
            q = a + u * sx - out * 0.14
            _box(m, q[0], q[1], u, 0.02, 0.32, z0 - 0.4, spring, col * 0.9, top=False)


def armes_arcade(m, poly, key, col, kind, style, fh, mntR, stats):
    """The arcade of a building on the square: the arcade edges' walls start
    above it (add_walls, through ARCADE_NOW), and here the arch face, the
    shop wall at the back, the ceiling and the pale stone floor."""
    global ARCADE_NOW
    spec = ARCADES[key]
    sq = ARMES["poly"]
    ring = np.asarray(orient(poly, 1.0).exterior.coords)
    lines, n_ = [], 0
    zs_all = []
    for a, b in zip(ring[:-1], ring[1:]):
        L = float(np.linalg.norm(b - a))
        if L < 6:
            continue
        u = (b - a) / L
        out = np.array([u[1], -u[0]])
        mid = (a + b) / 2
        if sq.distance(Point(*(mid + out * 2.0))) > 1.5:
            continue
        nb = max(1, int(round(L / spec["bay"])))
        s = (np.arange(nb) + 0.5) / nb
        pp = a[None] + (b - a)[None] * s[:, None] + out[None] * 1.5
        zf = mntR.sample(pp[:, 0], pp[:, 1])
        if not np.isfinite(zf).any():
            continue
        zf = np.where(np.isfinite(zf), zf, np.nanmedian(zf)) + 0.05
        ztop = float(zf.max()) + spec["h"]
        _arch_face(m, a, b, zf, ztop, nb, spec["pier"], np.array(hexrgb("#efe9dc")), out)
        # Back wall (shops, lit), ceiling, floor.
        d = spec["d"]
        back_a, back_b = a - out * d, b - out * d
        strip = orient(Polygon([a, b, back_b, back_a]), 1.0)
        zlo = float(zf.min())
        add_walls(m, strip, zlo - 0.3, ztop, zlo, max(3.0, ztop - zlo), col * 0.92, kind, inward=True)
        nbk = max(1, int(round(L / spec["bay"])))
        for i in range(nbk):
            q = back_a + (back_b - back_a) * (i + 0.5) / nbk + out * 0.04
            z0_ = float(zf[min(i, len(zf) - 1)])
            _vpanel(m, q, u, -out, [(-spec["bay"] / 2 + 0.6, z0_), (spec["bay"] / 2 - 0.6, z0_),
                                    (spec["bay"] / 2 - 0.6, z0_ + 3.2), (-spec["bay"] / 2 + 0.6, z0_ + 3.2)],
                    np.array(hexrgb("#e3bd82")), depth=0.02, style=LIT_GLASS)
        t_ = triangulate(strip)
        if t_ is not None:
            v, tri = t_
            P = enu_fast(v[:, 0], v[:, 1], np.full(len(v), ztop - 0.02))
            _quad_tris(m, P, tri, np.array(hexrgb("#e8e0cf")) * 0.8, down=True)
            zfl = mntR.sample(v[:, 0], v[:, 1])
            zfl = np.where(np.isfinite(zfl), zfl, zlo) + 0.06
            P = enu_fast(v[:, 0], v[:, 1], zfl)
            _quad_tris(m, P, tri, np.array(hexrgb("#ddd5c6")), down=False)
        lines.append(LineString([a, b]))
        zs_all.append(ztop)
        n_ += nb
    ARCADE_NOW = {"lines": lines, "z": max(zs_all)} if lines else None
    stats["armes_arches"] = stats.get("armes_arches", 0) + n_
    return n_


def _quad_tris(m, P, tri, col, down):
    A, B_, C = P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]]
    nn = np.cross(B_ - A, C - A)
    up = np.array([0.0, 0.0, 1.0])
    flip = ((nn @ up) > 0) == down
    B2 = np.where(flip[:, None], C, B_)
    C2 = np.where(flip[:, None], B_, C)
    m.tri(A, B2, C2, col, PLAIN)


def armes_paving(gm, mntR, gz, stats):
    """The square and the Rampe Major, as slabs over the ground mesh in
    style HERRING: TEXCOORD_1 holds the grid's metres (the viewer lays the
    bricks and the bands)."""
    pieces = [(ARMES["poly"], None)]                # the Rampe Major: rampe_major() builds its steps
    area = 0.0
    for g, line in pieces:
        for q in to_polys(g):
            q = shapely.set_precision(q.simplify(0.05), 0.01)
            if q.is_empty or q.geom_type != "Polygon" or q.area < 2 or not q.is_valid:
                continue
            tr = triangulate(orient(q, 1.0), area=3)
            if tr is None:
                continue
            v, tri = tr
            z = mntR.sample(v[:, 0], v[:, 1])
            if gz is not None:
                fi, fj = (v[:, 0] - X0) / GRID, (Y1 - v[:, 1]) / GRID
                z = np.fmax(z, ndimage.map_coordinates(gz, [fj, fi], order=1, mode="nearest"))
            ok = np.isfinite(z)
            if not ok.all():
                z = np.where(ok, z, np.nanmedian(z))
            zt = z[tri]
            tri = tri[(zt.max(1) - zt.min(1)) < 2.0]
            P = enu_fast(v[:, 0], v[:, 1], z + 0.08)
            uv = np.column_stack([(v[:, 0] - X0) / (X1 - X0), (Y1 - v[:, 1]) / (Y1 - Y0)])
            if line is None:
                uv2 = armes_grid(v[:, 0], v[:, 1])
            else:
                # Along the ramp, and across it from one edge: the grey
                # stone edges and the steps' nosings are the bands.
                s_ = shapely.line_locate_point(line, shapely.points(v))
                cp = shapely.line_interpolate_point(line, s_)
                dx, dy = v[:, 0] - shapely.get_x(cp), v[:, 1] - shapely.get_y(cp)
                tg = np.array(line.coords[-1]) - np.array(line.coords[0])
                side = np.sign(tg[0] * dy - tg[1] * dx)
                uv2 = np.column_stack([s_, side * np.hypot(dx, dy) + ARMES_BAND / 2])
            gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=tri, style=HERRING, uv2=uv2)
            area += q.area
    stats["armes_paved_m2"] = round(area)
    return round(area)


RAMPE_W = 3.6                  # the Rampe Major's width, kerb to kerb (constant; the hairpin's legs are ~5 m apart)
RAMPE = None                   # {"line", "poly"}: the route from Place d'Armes to Place du Palais


def rampe_clamp(X, Y, Z):
    """Ground nodes under the Rampe Major (and 1.2 m either side) kept
    below its treads, so the ramp is never buried in the slope it climbs;
    its uphill side gets a retaining wall instead (rampe_major())."""
    if RAMPE is None or "z" not in RAMPE:
        return Z
    from scipy.spatial import cKDTree
    x0, y0, x1, y1 = RAMPE["line"].buffer(RAMPE_W / 2 + 1.5).bounds
    sel = (X >= x0) & (X <= x1) & (Y >= y0) & (Y <= y1)
    if not sel.any():
        return Z
    d, j = cKDTree(RAMPE["P"]).query(np.column_stack([X[sel], Y[sel]]))
    near = d < RAMPE_W / 2 + 1.2
    Z = Z.copy()
    v = Z[sel]
    v[near] = np.minimum(v[near], RAMPE["zt"][j[near]] - 0.25)
    Z[sel] = v
    return Z


def rampe_heights(mnt, ways):
    """The ramp's centre line resampled every 0.5 m and its height there:
    LiDAR ground (median round each point), bridged under the gates'
    vaults, kept climbing."""
    if RAMPE is None:
        return
    mntR = Raster(mnt)
    ln = RAMPE["line"]
    L = ln.length
    n_ = max(4, int(L / 0.5))
    ss = np.linspace(0.0, L, n_ + 1)
    P = np.array([ln.interpolate(x).coords[0] for x in ss])
    off = np.array([[dx, dy] for dx in (-0.8, 0, 0.8) for dy in (-0.8, 0, 0.8)])
    z = np.array([np.nanmedian(mntR.sample(*(p_[None] + off).T)) for p_ in P])
    cov = np.zeros(len(P), bool)
    for t, p_, c in ways:
        if t.get("name") == "Rampe Major" and (t.get("tunnel") or t.get("covered") == "yes" or t.get("layer", "0") != "0"):
            cov |= contains_xy(LineString(p_).buffer(0.6), P[:, 0], P[:, 1])
    z[cov] = np.nan
    ok = np.isfinite(z)
    if ok.sum() < 2:
        return
    z = np.maximum.accumulate(np.interp(ss, ss[ok], z[ok]))
    k_ = max(1, int(round(ARMES_BAND / 0.5)))
    zt = z.copy()
    for i0 in range(0, len(P) - 1, k_):
        i1 = min(i0 + k_, len(P) - 1)
        f = (ss[i0:i1 + 1] - ss[i0]) / max(1e-6, ss[i1] - ss[i0])
        zt[i0:i1 + 1] = z[i0] + (z[i1] - z[i0]) * 0.35 * f + 0.12
    RAMPE.update({"ss": ss, "P": P, "z": z, "zt": zt, "cov": cov})


def rampe_setup(ways):
    """The Rampe Major as one route: every OpenStreetMap piece of it
    (open flights, the passages under the two gates, the last covered
    flight onto Place du Palais) merged into one line, bottom first."""
    from shapely.ops import linemerge
    parts = [LineString(p_) for t, p_, c in ways if t.get("name") == "Rampe Major" and len(p_) >= 2 and not c]
    if not parts:
        return None
    mg = linemerge(unary_union(parts))
    lines = list(getattr(mg, "geoms", [mg]))
    ln = max(lines, key=lambda g: g.length)
    a_ = np.array(ln.coords[0])
    if np.hypot(*(a_ - np.array(ARMES_C))) > np.hypot(*(np.array(ln.coords[-1]) - np.array(ARMES_C))):
        ln = LineString(ln.coords[::-1])
    return {"line": ln, "poly": ln.buffer(RAMPE_W / 2, cap_style=2, join_style=2, mitre_limit=2.0)}


def _ribbon_edges(P, W):
    """Left and right edges of a constant-width ribbon round polyline P,
    with mitred joints (capped at 2x) so every cross-section is W wide."""
    d = np.diff(P, axis=0)
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    n_seg = np.column_stack([-d[:, 1], d[:, 0]])
    N = np.zeros_like(P)
    N[0], N[-1] = n_seg[0], n_seg[-1]
    for i in range(1, len(P) - 1):
        m_ = n_seg[i - 1] + n_seg[i]
        nm = np.linalg.norm(m_)
        if nm < 1e-6:
            N[i] = n_seg[i]
            continue
        m_ /= nm
        N[i] = m_ / max(0.5, float(m_ @ n_seg[i]))
    return P + N * W / 2, P - N * W / 2


def rampe_major(gm, m, ways, mnt, stats):
    """The Rampe Major, the stepped ramp from Place d'Armes up to Place du
    Palais (street photos 2016-2025), as one ribbon of constant width
    along the whole route (OpenStreetMap, bottom to top, through the two
    gates): long gently sloping treads of red herringbone brick between
    grey stone edges, each ending in one low stone step (a pas-d'âne), a
    rubble parapet with a pale coping on the open side."""
    if RAMPE is None:
        return 0
    mntR = Raster(mnt)
    if "z" not in RAMPE:
        return 0
    W = RAMPE_W
    ss, P, z, cov = RAMPE["ss"], RAMPE["P"], RAMPE["z"], RAMPE["cov"]
    n_ = len(P) - 1
    L = float(ss[-1])
    Lft, Rgt = _ribbon_edges(P, W)
    stone = np.array(hexrgb("#a9a49a"), float)
    rubble = np.array(hexrgb(WALL_RUBBLE), float)
    cope = np.array(hexrgb("#e2ddd2"), float)
    # Pas-d'âne: treads of ~4.7 m (the grey bands' period), each climbing
    # a third of its rise, then a step.
    k_tread = max(1, int(round(ARMES_BAND / 0.5)))
    bounds = list(range(0, n_, k_tread)) + [n_]
    zt = np.empty(len(P))
    n_steps = 0
    for i0, i1 in zip(bounds[:-1], bounds[1:]):
        za, zb = z[i0], z[i1]
        f = (ss[i0:i1 + 1] - ss[i0]) / max(1e-6, ss[i1] - ss[i0])
        zt[i0:i1 + 1] = za + (zb - za) * 0.35 * f + 0.12
        if zb - za > 0.05:
            n_steps += 1
    # Tread tops: one strip of quads per tread (the step is the jump
    # between this tread's end and the next one's start).
    for i0, i1 in zip(bounds[:-1], bounds[1:]):
        za_end = z[i0] + (z[i1] - z[i0]) * 0.35 + 0.12
        idx = np.arange(i0, i1 + 1)
        zz = z[i0] + (z[i1] - z[i0]) * 0.35 * (ss[idx] - ss[i0]) / max(1e-6, ss[i1] - ss[i0]) + 0.12
        V = np.concatenate([Lft[idx], Rgt[idx]])
        Zv = np.concatenate([zz, zz])
        Pv = enu_fast(V[:, 0], V[:, 1], Zv)
        uv = np.column_stack([(V[:, 0] - X0) / (X1 - X0), (Y1 - V[:, 1]) / (Y1 - Y0)])
        k = len(idx)
        uv2 = np.column_stack([np.concatenate([ss[idx], ss[idx]]), np.concatenate([np.zeros(k), np.full(k, W)]) + (ARMES_BAND - W) / 2])
        tri = []
        for j in range(k - 1):
            tri += [[j, j + 1, k + j + 1], [j, k + j + 1, k + j]]
        tri = np.array(tri)
        # Wind upward.
        a_, b_, c_ = Pv[tri[:, 0]], Pv[tri[:, 1]], Pv[tri[:, 2]]
        up = np.cross(b_ - a_, c_ - a_)[:, 2] < 0
        tri[up] = tri[up][:, [0, 2, 1]]
        gm.add(Pv, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=tri, style=HERRING, uv2=uv2)
        # The riser at its top end, up to the next tread's start.
        if i1 < n_:
            z_next = z[i1] + 0.12
            if z_next - za_end > 0.03:
                A, B = Lft[i1], Rgt[i1]
                Pq = enu_fast(np.array([A[0], B[0], B[0], A[0]]), np.array([A[1], B[1], B[1], A[1]]),
                              np.array([za_end, za_end, z_next, z_next]))
                back = P[max(0, i1 - 1)] - P[i1]
                w_ = enu_fast(np.array([P[i1][0] + back[0]]), np.array([P[i1][1] + back[1]]), np.array([za_end]))[0] - \
                    enu_fast(np.array([P[i1][0]]), np.array([P[i1][1]]), np.array([za_end]))[0]
                _quad(m, Pq[0][None], Pq[1][None], Pq[2][None], Pq[3][None], stone, w_[None])
    # Kerbs (the grey stone edges) and the side faces down into the ground;
    # a rubble parapet on whichever side the ground falls away.
    n_par = 0.0
    # Uphill walls' tops: the slope 1.8 m out, smoothed along the ramp.
    wtop = {}
    for E_, sg in ((Lft, 1.0), (Rgt, -1.0)):
        o_ = P + (E_ - P) * (1.0 + 1.8 / (W / 2))
        gu = mntR.sample(o_[:, 0], o_[:, 1])
        w_ = np.fmax(np.nan_to_num(gu, nan=-99.0) + 0.2, zt + 0.6)
        w_ = ndimage.maximum_filter1d(w_, 9)
        wtop[sg] = ndimage.uniform_filter1d(w_, 9)
    for E_, sg in ((Lft, 1.0), (Rgt, -1.0)):
        for j in range(len(P) - 1):
            if cov[j] and cov[j + 1]:
                continue
            a2, b2 = E_[j], E_[j + 1]
            za, zb = zt[j], zt[j + 1]
            d_ = b2 - a2
            Ls = float(np.hypot(*d_))
            if Ls < 1e-3:
                continue
            F = Frame(m, a2, d_)
            out_ = sg * -1.0                       # Frame's t points right of travel; left edge is out at -t
            F.quad((0.0, 0.0, za - 0.6), (Ls, 0.0, zb - 0.6), (Ls, 0.0, zb + 0.08), (0.0, 0.0, za + 0.08), stone,
                   (0, out_, 0))
            o = P[j] + (E_[j] - P[j]) * (1.0 + 2.5 / (W / 2))
            gz = mntR.sample(np.array([o[0]]), np.array([o[1]]))[0]
            if np.isfinite(gz) and gz < min(za, zb) - 1.0:
                t0, t1 = sorted((0.0, out_ * 0.45))
                ztop = max(za, zb) + 1.0
                F.box(0.0, Ls, t0, t1, min(za, zb) - 1.5, ztop, rubble, top=False, style=RUBBLE, uvm=0.45)
                F.box(-0.02, Ls + 0.02, t0 - 0.05, t1 + 0.05, ztop, ztop + 0.14, cope)
                n_par += Ls
            else:
                # Uphill: a rubble retaining wall holding the slope back
                # from the ramp (the ground under the ramp was lowered),
                # its top following the slope smoothly.
                wa, wb = wtop[sg][j], wtop[sg][j + 1]
                for tt in (0.0, out_ * 0.5):
                    F.quad((0.0, tt, za - 0.4), (Ls, tt, zb - 0.4), (Ls, tt, wb), (0.0, tt, wa), rubble,
                           (0, -out_ if tt == 0.0 else out_, 0), style=RUBBLE, uvm=0.45)
                F.quad((0.0, 0.0, wa), (Ls, 0.0, wb), (Ls, out_ * 0.5, wb), (0.0, out_ * 0.5, wa), cope, (0, 0, 1))
    stats["rampe_major_steps"] = n_steps
    stats["rampe_major_parapet_m"] = round(n_par)
    stats["rampe_major_m"] = round(L)
    return n_steps


# ------------------------------------------------ Saint-Nicolas Cathedral ----
CATH = "BATIMENT0000000074078980"
CATH_O = (1056200.0, 6302309.5)           # the west end of the south front, on its plane (20 cm photo)
CATH_U = (1.0, 0.0)                       # along the front, west to east; the nave runs north
CATH_STONE = "#ebe6da"                    # white La Turbie stone (street photos)
CATH_ROOF = "#b2684b"


def cathedrale(m, gm, mntR, stats):
    """Saint-Nicolas Cathedral (1875-1903), Romanesque Revival, in a frame
    on its front: s along it from the west (0) to the east, t out onto the
    forecourt (the nave runs to t = -66, the chevet to -76), z m NGF. Plan
    and heights from the LiDAR and the 20 cm photo (the front block, the
    nave and aisles, the transept, the octagon over the crossing, the two
    square towers by the choir, the apse in its ring of chapels); the
    front from the 2015 and 2019 street photos: three portals, a gallery
    of arches, the rose window under a steep gable, round corner turrets."""
    F = Frame(m, CATH_O, CATH_U)
    zg = mntR.sample(np.array([F.xy(15.5, 5.0)[0]]), np.array([F.xy(15.5, 5.0)[1]]))[0]
    Zg = float(zg) if np.isfinite(zg) else 60.0           # the forecourt
    F0 = Zg + 1.6                                          # the floor, up the steps
    zb = Zg - 1.0                                          # walls start below the ground
    st = np.array(hexrgb(CATH_STONE), float)
    lit, shade = st * 1.03, st * 0.92
    roof = np.array(hexrgb(CATH_ROOF), float)
    dark = np.array(hexrgb("#2b2723"), float)
    n_win = 0

    # -- The front block: a centre bay under the gable, two side bays. ------
    CB = (10.0, 21.0)
    SB = ((2.5, 10.0), (21.0, 29.5))
    FT = -13.0
    F.box(CB[0], CB[1], FT, 0.0, zb, F0 + 23.0, st, top=False)
    F.gable_roof(CB[0], CB[1], FT, 0.0, F0 + 23.0, 4.5, roof, wall=st, along="t", over=0.3)
    for s0, s1 in SB:
        F.box(s0, s1, FT, 0.0, zb, F0 + 22.0, st)
        F.slab("+t", 0.0, s0 - 0.2, s1 + 0.2, F0 + 21.2, F0 + 22.0, 0.45, lit)        # cornice
        for k in range(int((s1 - s0) / 0.7)):                                            # arched corbel table
            a_ = s0 + 0.35 + k * 0.7
            F.slab("+t", 0.0, a_ - 0.1, a_ + 0.1, F0 + 20.4, F0 + 21.2, 0.2, lit)
        F.slab("+t", 0.0, s0, s1, F0 + 20.25, F0 + 20.45, 0.25, lit)
        if s0 < 5:
            F.slab("-s", s0, FT, 0.2, F0 + 21.2, F0 + 22.0, 0.45, lit)
        else:
            F.slab("+s", s1, FT, 0.2, F0 + 21.2, F0 + 22.0, 0.45, lit)
        for sp in (s0 + 0.5, s1 - 0.5):                                                 # corner pinnacles
            F.box(sp - 0.4, sp + 0.4, -0.9, -0.1, F0 + 22.0, F0 + 23.6, lit)
            F.cone(sp, -0.5, F0 + 23.6, 0.55, 1.4, lit, n=8, kind=PLAIN)
    # The steep front gable, standing a little proud, its edge in arcading.
    gz0, gz1 = F0 + 21.5, F0 + 28.5
    F.slab("+t", 0.0, CB[0] - 0.2, CB[1] + 0.2, F0, gz0, 0.35, st)
    F.tri((CB[0] - 0.2, 0.35, gz0), (CB[1] + 0.2, 0.35, gz0), (15.5, 0.35, gz1), st, (0, 1, 0))
    for sg in (-1, 1):                                                                  # coping up the rakes
        e = 15.5 + sg * 5.9
        lo = lambda tt, dz=0.0: [(e, tt, gz0 - 0.1 + dz), (15.5, tt, gz1 - 0.1 + dz)]
        (p0, p1), (q0, q1) = lo(0.6), lo(0.6, 0.5)
        F.quad(p0, p1, q1, q0, lit, (0, 1, 0))
        (r0, r1) = lo(-0.1, 0.5)
        F.quad(q0, q1, r1, r0, lit * 1.02, (sg * (gz1 - gz0), 0, 5.9))
        (u0, u1) = lo(-0.1)
        F.quad(p0, p1, u1, u0, shade, (-sg * (gz1 - gz0), 0, -5.9))
        # A frieze of little blind arches under the coping.
        for k in range(9):
            f = (k + 0.5) / 9
            a_ = e + (15.5 - e) * f
            z_ = gz0 + (gz1 - gz0) * f - 0.75
            F.slab("+t", 0.35, a_ - 0.12, a_ + 0.12, z_ - 0.5, z_, 0.12, lit)
    F.box(15.1, 15.9, -0.1, 0.5, gz1, gz1 + 1.1, lit)                                  # the angel on the apex
    F.column(15.5, 0.2, gz1 + 1.1, gz1 + 2.4, 0.22, lit)
    _ball(m, *F.xy(15.5, 0.2), gz1 + 2.6, 0.3, lit, None, 8, 5)
    # Rose window under the gable.
    F.arch_glass("+t", 0.35, 15.5, 4.2, F0 + 17.2, 0, 7, nseg=16, th1=2 * math.pi)
    F.arch_ring("+t", 0.35, 15.5, 4.2, F0 + 17.2, 0.3, 0.45, lit, nseg=20, th1=2 * math.pi)
    for k in range(8):                                                                  # tracery spokes
        x = k * math.pi / 8
        F.slab("+t", 0.38, 15.5 + 1.9 * math.cos(x) - 0.06, 15.5 + 1.9 * math.cos(x) + 0.06,
               F0 + 17.2 + 1.9 * math.sin(x) - 0.06, F0 + 17.2 + 1.9 * math.sin(x) + 0.06, 0.05, lit)
    F.arch_ring("+t", 0.38, 15.5, 1.0, F0 + 17.2, 0.12, 0.25, lit, nseg=12, th1=2 * math.pi)
    n_win += 1
    # The gallery: a row of five round arches on little columns.
    F.slab("+t", 0.35, CB[0], CB[1], F0 + 10.6, F0 + 11.1, 0.35, lit)
    for k in range(5):
        a = CB[0] + 1.3 + k * (CB[1] - CB[0] - 2.6) / 4
        F.window("+t", 0.35, a, 1.25, F0 + 11.1, F0 + 13.4, k, 3, frame=lit, arch=True, band=0.18, sill=False)
        n_win += 1
    F.slab("+t", 0.35, CB[0], CB[1], F0 + 14.2, F0 + 14.6, 0.3, lit)

    def portal(a, w, zs, depth, rings, gable):
        """A portal: the door under stepped arches on nook shafts, in a
        porch standing proud of the front, under a little gable."""
        nonlocal n_win
        c = 0.35 if CB[0] < a < CB[1] else 0.0
        F.face_quad("+t", c, a - w / 2, a + w / 2, F0, zs, dark, out=0.02)
        F.arch_glass("+t", c, a, w, zs, 0, 0, nseg=10, dark=True)
        bw = w / 2 + rings * 0.32
        for side in (-1, 1):                                                        # the jambs
            F.slab("+t", c, *sorted((a + side * w / 2, a + side * (bw + 0.6))), F0, zs, depth, st)
            for k in range(rings):
                F.column(a + side * (w / 2 + 0.16 + k * 0.32), c + depth * (k + 0.5) / (rings + 0.5),
                         F0 + 0.4, zs - 0.3, 0.13, lit)
        for k in range(rings):                                                       # archivolts
            F.arch_ring("+t", c, a, w + 2 * k * 0.32, zs, depth * (k + 1) / rings, 0.32, lit * (1 - 0.02 * k), nseg=14)
        F.slab("+t", c, a - bw - 0.6, a + bw + 0.6, zs + bw, zs + bw + 0.5, depth, st)
        F.slab("+t", c, a - bw - 0.6, a - bw, zs, zs + bw, depth, st)
        F.slab("+t", c, a + bw, a + bw + 0.6, zs, zs + bw, depth, st)
        if gable:
            zt = zs + bw + 0.5
            F.tri((a - bw - 0.6, c + depth, zt), (a + bw + 0.6, c + depth, zt), (a, c + depth, zt + bw * 0.7), lit, (0, 1, 0))
            F.gable_roof(a - bw - 0.6, a + bw + 0.6, c, c + depth, zt, bw * 0.7, lit, along="t", over=0.15)
        n_win += 1

    portal(15.5, 3.2, F0 + 4.6, 1.3, 4, True)
    for s0, s1 in SB:
        a = (s0 + s1) / 2
        portal(a, 1.9, F0 + 3.5, 0.8, 2, False)
        for i, (z0, z1) in enumerate(((F0 + 8.4, F0 + 11.2), (F0 + 14.2, F0 + 17.6))):
            for d in (-0.95, 0.95):                                                  # twin arched windows
                F.window("+t", 0.0, a + d, 1.15, z0, z1, int(a), i + 1, frame=lit, arch=True, band=0.2, sill=False)
                n_win += 1
            F.column(a, 0.15, z0, z1, 0.12, lit)
            F.slab("+t", 0.0, s0, s1, z0 - 0.9, z0 - 0.5, 0.25, lit)
    for a in (CB[0], CB[1]):                                                         # buttresses
        F.slab("+t", 0.0, a - 0.55, a + 0.55, zb, F0 + 21.5, 0.75, st)
        F.box(a - 0.5, a + 0.5, 0.0, 0.7, F0 + 21.5, F0 + 23.4, lit)
        F.cone(a, 0.35, F0 + 23.4, 0.6, 1.8, lit, n=8, kind=PLAIN)
    F.slab("+t", 0.0, 2.5, 29.5, F0 - 0.2, F0 + 0.4, 0.4, lit)                     # plinth

    # Round turrets on the front's corners, domed, with little arcades.
    for (sc, tc, r) in ((1.3, -4.5, 2.0), (31.0, -6.0, 2.4)):
        F.drum(sc, tc, zb, F0 + 9.0, r, st, n=14)
        F.drum(sc, tc, F0 + 9.0, F0 + 9.4, r + 0.25, lit, n=14)
        F.drum(sc, tc, F0 + 9.4, F0 + 11.2, r - 0.1, st * 0.97, n=14)
        for k in range(10):
            x = k * 2 * math.pi / 10
            p = (sc + (r - 0.05) * math.cos(x), tc + (r - 0.05) * math.sin(x))
            F.column(p[0], p[1], F0 + 9.4, F0 + 11.2, 0.09, lit)
        F.drum(sc, tc, F0 + 11.2, F0 + 11.6, r + 0.2, lit, n=14)
        F.dome(sc, tc, F0 + 11.6, r + 0.05, r * 0.8, lit, n=14)
        _ball(m, *F.xy(sc, tc), F0 + 11.6 + r * 0.8 + 0.25, 0.22, lit, None, 8, 5)

    # -- Nave, aisles, transept, crossing, towers, choir, chevet. -----------
    NV = (10.5, 20.5)
    AI = ((3.5, NV[0]), (NV[1], 27.5))
    T0, T1 = FT, -40.0
    F.box(NV[0], NV[1], T1, T0, zb, F0 + 19.5, st, top=False)
    F.gable_roof(NV[0], NV[1], T1, T0, F0 + 19.5, 2.6, roof, along="t")
    nbay = 6
    for (s0, s1), face in zip(AI, ("-s", "+s")):
        F.box(s0, s1, T1, T0, zb, F0 + 11.0, st, top=False)
        outer, inner = (s0, s1) if face == "-s" else (s1, s0)
        # Lean-to roof from the outer eaves up to the nave wall.
        F.tri((outer - (0.4 if face == "-s" else -0.4), T1, F0 + 10.8), (inner, T1, F0 + 13.0), (inner, T0, F0 + 13.0),
              roof, (-1 if face == "-s" else 1, 0, 3), ROOF_TILE)
        F.tri((outer - (0.4 if face == "-s" else -0.4), T1, F0 + 10.8), (inner, T0, F0 + 13.0),
              (outer - (0.4 if face == "-s" else -0.4), T0, F0 + 10.8), roof, (-1 if face == "-s" else 1, 0, 3), ROOF_TILE)
        F.slab(face, outer, T1, T0, F0 + 10.5, F0 + 11.0, 0.35, lit)                 # eaves cornice
        nf = "-s" if face == "-s" else "+s"
        cl = NV[0] if face == "-s" else NV[1]
        for k in range(nbay):
            a = T0 - (k + 0.5) * (T0 - T1) / nbay
            F.window(face, outer, a, 1.3, F0 + 4.2, F0 + 7.6, k, 1, frame=lit, arch=True, band=0.22, sill=True)
            F.window(nf, cl, a - 0.85, 0.9, F0 + 14.2, F0 + 16.8, k, 2, frame=lit, arch=True, band=0.16, sill=False)
            F.window(nf, cl, a + 0.85, 0.9, F0 + 14.2, F0 + 16.8, k, 2, frame=lit, arch=True, band=0.16, sill=False)
            n_win += 3
            b = T0 - k * (T0 - T1) / nbay
            if k:
                F.slab(face, outer, b - 0.45, b + 0.45, zb, F0 + 10.5, 0.7, st)        # buttresses
        F.slab(nf, cl, T1, T0, F0 + 19.0, F0 + 19.5, 0.35, lit)                        # nave cornice
        F.slab(face, outer, T1, T0, F0 - 0.3, F0 + 0.4, 0.3, lit)
    # Transept: gabled arms, round windows in the gable ends.
    TR = (0.5, 32.0)
    TT = (-50.0, T1)
    F.box(TR[0], TR[1], TT[0], TT[1], zb, F0 + 19.5, st, top=False)
    F.gable_roof(TR[0], TR[1], TT[0], TT[1], F0 + 19.5, 3.2, roof, wall=st, along="s")
    tm = (TT[0] + TT[1]) / 2
    for face, c in (("-s", TR[0]), ("+s", TR[1])):
        F.arch_glass(face, c, tm, 3.2, F0 + 14.5, 0, 7, nseg=14, th1=2 * math.pi)
        F.arch_ring(face, c, tm, 3.2, F0 + 14.5, 0.3, 0.4, lit, nseg=16, th1=2 * math.pi)
        for d in (-1.6, 1.6):
            F.window(face, c, tm + d, 1.2, F0 + 5.0, F0 + 9.0, 7, 1, frame=lit, arch=True, band=0.22)
        F.slab(face, c, TT[0], TT[1], F0 + 19.0, F0 + 19.5, 0.35, lit)
        for b in (TT[0] + 0.4, TT[1] - 0.4):
            F.slab(face, c, b - 0.5, b + 0.5, zb, F0 + 19.0, 0.6, st)
        n_win += 3
    # The octagon over the crossing: a drum with arched openings, a low roof.
    oc = (15.3, -46.5)
    F.drum(*oc, F0 + 19.0, F0 + 26.6, 6.3, st, n=8, th0=math.pi / 8, th1=2 * math.pi + math.pi / 8)
    for k in range(8):
        x = math.pi / 8 + (k + 0.5) * math.pi / 4
        fc = (oc[0] + 5.85 * math.cos(x), oc[1] + 5.85 * math.sin(x))
        nrm = (math.cos(x), math.sin(x), 0.0)
        P = [F.xy(fc[0] + 0.85 * -math.sin(x) * sg, fc[1] + 0.85 * math.cos(x) * sg) for sg in (-1, 1)]
        q = [(fc[0] + 0.85 * -math.sin(x) * sg + 0.02 * math.cos(x), fc[1] + 0.85 * math.cos(x) * sg + 0.02 * math.sin(x))
             for sg in (-1, 1)]
        F.quad((*q[0], F0 + 21.2), (*q[1], F0 + 21.2), (*q[1], F0 + 24.4), (*q[0], F0 + 24.4), dark, nrm)
    F.drum(*oc, F0 + 26.6, F0 + 27.1, 6.6, lit, n=8, th0=math.pi / 8, th1=2 * math.pi + math.pi / 8)
    F.cone(*oc, F0 + 27.1, 6.9, 2.4, roof, n=8, th0=math.pi / 8, th1=2 * math.pi + math.pi / 8)
    F.column(oc[0], oc[1], F0 + 29.4, F0 + 30.4, 0.12, lit)
    # The two square towers by the choir.
    for s0, s1 in ((6.0, 13.5), (20.0, 27.5)):
        F.box(s0, s1, -57.5, -50.0, zb, F0 + 29.6, st, top=False)
        sm = (s0 + s1) / 2
        for face, c, a in (("-s", s0, -53.75), ("+s", s1, -53.75), ("-t", -57.5, sm)):
            for d in (-1.0, 1.0):
                F.window(face, c, a + d, 1.0, F0 + 23.8, F0 + 27.0, 9, 4, frame=lit, arch=True, band=0.18, sill=False)
            F.slab(face, c, *((-57.7, -49.8) if face[1] == "s" else (s0 - 0.2, s1 + 0.2)), F0 + 22.8, F0 + 23.2, 0.3, lit)
            F.slab(face, c, *((-57.8, -49.7) if face[1] == "s" else (s0 - 0.3, s1 + 0.3)), F0 + 29.2, F0 + 29.6, 0.45, lit)
        F.cone(sm, -53.75, F0 + 29.6, 5.6, 2.6, roof, n=4, th0=math.pi / 4, th1=2 * math.pi + math.pi / 4)
        n_win += 6
    # Choir, its rounded apse, the ambulatory and its chapels.
    AC = (15.5, -64.5)
    F.box(NV[0], NV[1], AC[1], -50.0, zb, F0 + 18.0, st, top=False, sides="tT")
    F.gable_roof(NV[0], NV[1], AC[1], -57.5, F0 + 18.0, 2.6, roof, along="t", over=0.2)
    F.drum(*AC, zb, F0 + 16.5, 5.0, st, n=12, th0=math.pi, th1=2 * math.pi)
    F.cone(*AC, F0 + 16.5, 5.3, 2.8, roof, n=12, th0=math.pi, th1=2 * math.pi)
    for sb in ((0.5, NV[0]), (NV[1], 31.0)):                                          # side chapels
        F.box(sb[0], sb[1], -66.0, -57.5, zb, F0 + 11.0, st)
        F.slab("-t", -66.0, sb[0], sb[1], F0 + 10.6, F0 + 11.0, 0.3, lit)
    F.drum(*AC, zb, F0 + 10.5, 11.0, st, n=16, th0=math.pi, th1=2 * math.pi)        # ambulatory
    pts, th = F._ring_pts(*AC, 11.4, 16, math.pi, 2 * math.pi)
    for i in range(16):
        xm = (th[i] + th[i + 1]) / 2
        (a0, b0), (a1, b1) = pts[i], pts[i + 1]
        i0 = (AC[0] + 5.0 * math.cos(th[i]), AC[1] + 5.0 * math.sin(th[i]))
        i1 = (AC[0] + 5.0 * math.cos(th[i + 1]), AC[1] + 5.0 * math.sin(th[i + 1]))
        F.tri((a0, b0, F0 + 10.3), (a1, b1, F0 + 10.3), (*i1, F0 + 13.0), roof, (math.cos(xm), math.sin(xm), 2.5), ROOF_TILE)
        F.tri((a0, b0, F0 + 10.3), (*i1, F0 + 13.0), (*i0, F0 + 13.0), roof, (math.cos(xm), math.sin(xm), 2.5), ROOF_TILE)
    for k in range(5):                                                                 # the radiating chapels
        x = math.pi + k * math.pi / 4
        cs, ct = AC[0] + 11.2 * math.cos(x), AC[1] + 11.2 * math.sin(x)
        F.drum(cs, ct, zb, F0 + 8.5, 2.9, st, n=10)
        F.drum(cs, ct, F0 + 8.5, F0 + 8.9, 3.1, lit, n=10)
        F.cone(cs, ct, F0 + 8.9, 3.2, 1.6, roof, n=10)
        for d in (-0.5, 0.0, 0.5):
            y = x + d
            p = (cs + 2.92 * math.cos(y), ct + 2.92 * math.sin(y))
            q = (cs + 2.92 * math.cos(y + 0.2), ct + 2.92 * math.sin(y + 0.2))
            F.quad((*p, F0 + 3.5), (*q, F0 + 3.5), (*q, F0 + 6.4), (*p, F0 + 6.4), dark, (math.cos(y), math.sin(y), 0))
    # The sacristy along the east side.
    F.box(27.5, 38.5, -53.0, -29.0, zb, F0 + 6.5, st * 0.97)
    F.slab("+s", 38.5, -53.0, -29.0, F0 + 6.1, F0 + 6.5, 0.3, lit)
    # The steps up from the forecourt.
    ns = 8
    for k in range(ns):
        F.box(-1.0, 33.0, 0.0, 4.2 - k * 0.45, zb, Zg + (k + 1) * (F0 - Zg) / ns, lit * (0.97 if k % 2 else 1.0),
              sides="stST")
    stats["cathedral_windows"] = n_win
    return F.poly(-1.5, 39.5, -77.0, 4.5)


# ------------------------------------------------- Le Rocher's old town ----
# The lanes of Monaco-Ville, after the 2019 street photos (Mapillary): the
# pedestrian lanes (Rue Basse, Rue Comte Félix Gastaldi, Rue Émile de Loth,
# the squares) in small grey setts with a few red ones; the streets cars use
# in asphalt with a red brick pavement along each house front.
ROCHER_ZONE = Polygon([(1056030, 6302255), (1056530, 6302255), (1056530, 6302560), (1056030, 6302560)])
ROCHER_GARDENS = None                  # the Rock's mapped gardens (rocher_gardens_setup)
SETT, PALAIS_BRICK = 12, 13           # GROUND styles: grey setts; Place du Palais's herringbone brick (no bands)
ROCHER_SETT_W = 7.0                    # m: the lanes' paving reaches the house fronts
ROCHER_PAVE_W = 1.3                    # m, brick pavement along the house fronts


def rocher_paved(t, p_):
    """A street of the old town paved in setts though OSM tags it a road
    (Place de la Mairie, Rue Princesse Marie de Lorraine): no asphalt."""
    return (t.get("surface") in ("paving_stones", "sett") and len(p_) >= 2
            and ROCHER_ZONE.contains(Point(*p_[len(p_) // 2])))


PALAIS_SUN_C = (1056044.95, 6302419.5)  # the sunburst's centre: before the gate, on its step (20 cm photo)
# The long grey sett panels across the square, traced from the 20 cm photo
# (the north one is the coaches' and cars' bay).
PALAIS_PANELS = [
    [(1056079.6, 6302424.7), (1056076.4, 6302427.7), (1056076.8, 6302430.5), (1056097.7, 6302439.2),
     (1056104.9, 6302439.2), (1056108.9, 6302433.2), (1056108.7, 6302427.7)],
    [(1056103.2, 6302397.5), (1056077.1, 6302409.0), (1056078.3, 6302418.6), (1056109.1, 6302424.3),
     (1056108.9, 6302404.8)],
    [(1056099.8, 6302386.3), (1056088.5, 6302385.8), (1056074.5, 6302396.0), (1056072.2, 6302400.3),
     (1056077.5, 6302402.4), (1056101.5, 6302393.9)],
    [(1056063.6, 6302391.7), (1056067.3, 6302391.7), (1056071.5, 6302381.0), (1056066.8, 6302376.8)],
]


def palais_pattern():
    """The grey in Place du Palais's red brick: in front of the gate a
    sunburst (a half disc of setts, eleven rays, kites and wedges in turn,
    and a thin grey arc round them), then the long radial panels."""
    c = np.array(PALAIS_SUN_C)
    F = Frame(None, PALACE_O, PALACE_U)
    ax = F.v                                   # out onto the square
    side = F.u
    out = [Point(*c).buffer(5.5, 48)]
    n = 11
    for k in range(n):
        a = math.radians(-90.0 + (k + 0.5) * 180.0 / n)
        d = ax * math.cos(a) + side * math.sin(a)
        w = np.array([-d[1], d[0]])
        if k % 2 == 0:                          # a wedge
            pts = [c + d * 7.3 + w * 0.45, c + d * 16.6 + w * 1.05, c + d * 16.6 - w * 1.05, c + d * 7.3 - w * 0.45]
        else:                                   # a kite
            pts = [c + d * 8.6, c + d * 11.8 + w * 1.25, c + d * 16.0, c + d * 11.8 - w * 1.25]
        out.append(Polygon(pts))
    out.append(Point(*c).buffer(18.2, 64).difference(Point(*c).buffer(17.8, 64)))
    half = Polygon([c - side * 30, c + side * 30, c + side * 30 + ax * 30, c - side * 30 + ax * 30])
    sun = unary_union(out).intersection(half)
    return unary_union([sun] + [Polygon(p) for p in PALAIS_PANELS])


def rocher_streets(gm, ways, mnt, gz, fps, stats):
    mntR = Raster(mnt)
    blds = unary_union([p for p in fps if p.intersects(ROCHER_ZONE)]).buffer(0.03)
    rp, _ = _road_polys(ways)
    carriage = unary_union([q for q in rp if q.intersects(ROCHER_ZONE)] or [Polygon()])
    keep_off = [Frame(None, PALACE_O, PALACE_U).poly(-60.0, 90.0, 0.5, 70.0)]
    sett_lines, road_lines, squares, palais_sq = [], [], [], []
    for t, p_, closed in ways:
        hw = t.get("highway")
        k = t.get("leisure") or t.get("landuse") or t.get("natural")
        if closed and len(p_) > 3 and k in ("park", "garden", "grass", "wood", "scrub"):
            keep_off.append(Polygon(p_).buffer(0))
        if closed and len(p_) > 3 and t.get("name") == "Place du Palais" and hw == "pedestrian":
            palais_sq.append(Polygon(p_).buffer(0))
        if not hw or t.get("tunnel") or t.get("layer", "0") != "0" or hw == "steps" or len(p_) < 2:
            continue
        ln = LineString(p_)
        if not ln.intersects(ROCHER_ZONE):
            continue
        if closed and hw == "pedestrian" and t.get("name") != "Place du Palais":
            squares.append(Polygon(p_).buffer(0))
        elif hw in ("footway", "pedestrian") or rocher_paved(t, p_):
            sett_lines.append(ln)
        elif hw in ("residential", "service", "living_street"):
            road_lines.append(ln)
    off = unary_union(keep_off).union(blds)
    # OSM's lanes run a few metres off BD TOPO's blocks here (some inside
    # them): a wide buffer, cut to the blocks, finds the real 3 m lanes.
    sett = unary_union([ln.buffer(ROCHER_SETT_W, cap_style=1) for ln in sett_lines] + squares)
    sett = sett.intersection(ROCHER_ZONE).difference(off).difference(carriage.buffer(0.05))
    side = unary_union([ln.buffer(8.0, cap_style=2) for ln in road_lines]).intersection(ROCHER_ZONE)
    side = side.intersection(blds.buffer(ROCHER_PAVE_W)).difference(off).difference(carriage).difference(sett)
    gardens = unary_union(keep_off[1:] or [Polygon()])
    sq = unary_union(palais_sq or [Polygon()]).difference(blds).difference(gardens).difference(carriage)
    # Red brick in herringbone (style PALAIS_BRICK: no grey bands), laid
    # square to the Palace's front (TEXCOORD_1, metres), with the
    # grey setts of the sunburst in front of the gate and the long radial
    # panels beyond (palais_pattern()).
    grey = sq.intersection(palais_pattern())
    Fp = Frame(None, PALACE_O, PALACE_U)

    def palais_grid(x, y):
        d = np.column_stack([x, y]) - Fp.O
        return np.column_stack([d @ Fp.u, d @ Fp.v])
    stats["palais_sq_m2"] = round(_slab(gm, sq.difference(grey), mntR, gz, PALAIS_BRICK, lift=0.09, uv2f=palais_grid))
    stats["palais_grey_m2"] = round(_slab(gm, grey, mntR, gz, SETT, lift=0.09))
    stats["rocher_sett_m2"] = round(_slab(gm, sett, mntR, gz, SETT, lift=0.09))
    stats["rocher_brick_m2"] = round(_slab(gm, side, mntR, gz, PAVER, lift=0.14))
    return stats["rocher_sett_m2"]


# ------------------------------------------- Fort Antoine, the ramparts ----
FORT = "BATIMENT0000000284089930"
FORT_C = (1056624.5, 6302672.0)          # the round fort's centre (BD TOPO outline, LiDAR rim)
FORT_STONE = "#cfc6b4"                   # pale limestone rubble


def fort_antoine(m, gm, mntR, stats):
    """Fort Antoine (1709), the round fort on the Rock's north-east tip,
    an open-air theatre since 1953: a rubble-stone drum from the road up to
    the platform (the LiDAR's 22 m floor and 24 m rim), a cordon, a plain
    parapet, a corbelled sentry box over the sea, stone tiers round the
    south half of the floor and a low stage on the north."""
    F = Frame(m, FORT_C, (1.0, 0.0))
    st = np.array(hexrgb(FORT_STONE), float)
    floor_ = 22.1
    th = np.linspace(0, 2 * math.pi, 33)
    R = 13.0
    zlow = mntR.sample(FORT_C[0] + (R + 2.0) * np.cos(th), FORT_C[1] + (R + 2.0) * np.sin(th))
    zb = float(np.nanmin(zlow)) - 0.5 if np.isfinite(zlow).any() else 9.0
    F.drum(0.0, 0.0, zb, floor_ + 0.6, R, st * 0.97, n=32, style=RUBBLE, uvm=0.45)
    F.drum(0.0, 0.0, floor_ + 0.6, floor_ + 1.0, R + 0.25, st * 1.04, n=32)          # the cordon
    F.drum(0.0, 0.0, floor_ + 1.0, floor_ + 2.2, R - 0.1, st, n=32, style=RUBBLE, uvm=0.45)
    F.drum(0.0, 0.0, floor_ + 2.2, floor_ + 2.4, R, st * 1.05, n=32)                 # coping
    # The parapet's inner face and its top.
    pts, _ = F._ring_pts(0.0, 0.0, R - 0.7, 32, 0.0, 2 * math.pi)
    out, _ = F._ring_pts(0.0, 0.0, R, 32, 0.0, 2 * math.pi)
    for i in range(32):
        xm = (th[i] + th[i + 1]) / 2
        (a0, b0), (a1, b1) = pts[i], pts[i + 1]
        (c0, d0), (c1, d1) = out[i], out[i + 1]
        F.quad((a0, b0, floor_), (a1, b1, floor_), (a1, b1, floor_ + 2.4), (a0, b0, floor_ + 2.4), st * 0.95,
               (-math.cos(xm), -math.sin(xm), 0))
        F.quad((a0, b0, floor_ + 2.4), (a1, b1, floor_ + 2.4), (c1, d1, floor_ + 2.4), (c0, d0, floor_ + 2.4), st * 1.05,
               (0, 0, 1))
    # The floor: a gravel arena.
    F.cone(0.0, 0.0, floor_, R - 0.7, 0.0, np.array(hexrgb("#cbbfa6")), n=32, kind=PLAIN)
    # Stone tiers round the south half, a low stage on the north.
    for k in range(4):
        r0, r1 = R - 0.7 - (k + 1) * 0.85, R - 0.7 - k * 0.85
        z = floor_ + (4 - k) * 0.42
        p0, _ = F._ring_pts(0.0, 0.0, r0, 14, math.pi * 1.05, math.pi * 1.95)
        p1, th2 = F._ring_pts(0.0, 0.0, r1, 14, math.pi * 1.05, math.pi * 1.95)
        for i in range(14):
            xm = (th2[i] + th2[i + 1]) / 2
            F.quad((*p0[i], z), (*p0[i + 1], z), (*p1[i + 1], z), (*p1[i], z), st * (1.02 - 0.03 * (k % 2)), (0, 0, 1))
            F.quad((*p0[i], floor_), (*p0[i + 1], floor_), (*p0[i + 1], z), (*p0[i], z), st * 0.88,
                   (-math.cos(xm), -math.sin(xm), 0))
    F.box(-4.5, 4.5, 4.0, 8.5, floor_, floor_ + 0.6, np.array(hexrgb("#8a6a4c")))       # the stage
    # A corbelled sentry box on the parapet, over the sea (east).
    sc, tc = R - 0.2, 0.0
    for k in range(4):
        F.drum(sc, tc, floor_ - 1.6 + k * 0.4, floor_ - 1.2 + k * 0.4, 0.5 + 0.15 * k, st, n=10)
    F.drum(sc, tc, floor_ + 0.0, floor_ + 3.6, 1.0, st * 1.02, n=12)
    F.drum(sc, tc, floor_ + 2.2, floor_ + 3.0, 1.03, np.array(hexrgb("#3a3631")), n=12, th0=-0.5, th1=0.5)
    F.dome(sc, tc, floor_ + 3.6, 1.1, 0.9, st * 1.05, n=12)
    _ball(m, *F.xy(sc, tc), floor_ + 4.65, 0.14, st, None, 6, 4)
    stats["fort_antoine"] = 1
    return F.poly(-R - 1, R + 1, -R - 1, R + 1)


def _cannon(m, x, y, z, deg, scale=1.0):
    """A bronze cannon on its wooden carriage, pointing to compass deg."""
    a = math.radians(deg)
    F = Frame(m, (x, y), (math.sin(a), math.cos(a)))
    wood = np.array(hexrgb("#3d2f25"), float)
    bronze = np.array(hexrgb("#6b5532"), float)
    F.box(-0.9 * scale, 0.5 * scale, -0.42 * scale, 0.42 * scale, z, z + 0.55 * scale, wood)
    for sg in (-1, 1):
        _cyl(m, *F.xy(0.1 * scale, sg * 0.5 * scale), z, z + 0.04, 0.42 * scale, wood * 0.8, sides=10)
    for j in range(7):
        r_ = (0.24 - 0.02 * j) * scale
        F.box((-1.0 + j * 0.42) * scale, (-0.58 + j * 0.42) * scale, -r_, r_, z + 0.7 * scale - r_, z + 0.7 * scale + r_,
              bronze)


def peche_terrace(gm, m, mnt, gz, stats):
    """The garden on the Parking des Pêcheurs' roof (25 m) and the walk at
    its foot: the sett walk runs round the sea side just above the rocks
    (OpenStreetMap, LiDAR 1-3 m), and from it the car park's sea wall rises
    to the garden, a battered face of limestone rubble found from the
    LiDAR's drop, with a parapet and coping on top."""
    mntR = Raster(mnt)
    E = LineString(PECHE_EDGE)
    n_ = max(2, int(E.length / 2.0))
    P = np.array([E.interpolate(E.length * i / n_).coords[0] for i in range(n_ + 1)])
    T = np.gradient(P, axis=0)
    T /= np.linalg.norm(T, axis=1, keepdims=True)
    N = np.column_stack([-T[:, 1], T[:, 0]])                # inland (the garden on the walk's left)
    ds = np.arange(0.0, 20.0, 0.25)
    foot, top, zf, zt = [], [], [], []
    for p_, n__ in zip(P, N):
        q = p_[None] + ds[:, None] * n__[None]
        z = np.nan_to_num(mntR.sample(q[:, 0], q[:, 1]), nan=0.0)
        zw = float(np.median(z[:4]))
        ztop = float(z.max())
        up = np.nonzero(z > zw + 1.5)[0]
        hi = np.nonzero(z >= ztop - 0.8)[0]
        if ztop < 15 or not len(up) or not len(hi) or hi[0] <= up[0]:
            foot.append(None); top.append(None); zf.append(np.nan); zt.append(np.nan)
            continue
        foot.append(p_ + n__ * max(0.0, ds[up[0]] - 0.6))
        top.append(p_ + n__ * max(0.0, ds[hi[0]] - 0.3))
        zf.append(zw)
        zt.append(ztop)
    stone = np.array(hexrgb(WALL_RUBBLE), float) * 1.02
    cope = np.array(hexrgb("#e9e4d8"), float)
    L_wall = 0.0
    for i in range(len(P) - 1):
        if top[i] is None or top[i + 1] is None:
            continue
        F = Frame(m, P[i], P[i + 1] - P[i])              # t out to sea
        st_ = lambda q: ((q - F.O) @ F.u, (q - F.O) @ F.v)
        za, zb = zt[i] + 0.05, zt[i + 1] + 0.05
        (s0, t0), (s1, t1) = st_(top[i]), st_(top[i + 1])
        (s2, t2), (s3, t3) = st_(foot[i]), st_(foot[i + 1])
        F.quad((s2, t2, zf[i] - 0.4), (s3, t3, zf[i + 1] - 0.4), (s1, t1, zb), (s0, t0, za), stone, (0, 1, 0.3),
               style=RUBBLE, uvm=0.45)
        # Parapet and coping just inside the top of the face.
        ta, L_ = min(t0, t1), float(np.hypot(*(P[i + 1] - P[i])))
        sa_, sb_ = min(s0, s1), max(s0, s1)
        F.box(sa_, sb_, ta - 0.45, ta, min(za, zb) - 0.3, max(za, zb) + 0.95, stone, top=False, style=RUBBLE, uvm=0.45)
        F.box(sa_ - 0.03, sb_ + 0.03, ta - 0.5, ta + 0.05, max(za, zb) + 0.95, max(za, zb) + 1.08, cope)
        L_wall += L_
    # The walk: grey setts, 3 m wide, at the foot of the wall.
    walk = E.buffer(1.5, cap_style=2)
    stats["peche_walk_m2"] = round(_slab(gm, walk, mntR, None, SETT, lift=0.07, area=1.5))
    stats["peche_seawall_m"] = round(L_wall)
    return round(L_wall)


def prison(m, poly, mntR, stats):
    """The Maison d'arrêt on the Rock's north-east tip, built down the
    cliff under Avenue des Pins: its roof terrace is about at the avenue
    (LiDAR 48.5 m), its walls drop to the Pêcheurs road (13 m). Pale grey
    limestone rubble like the ramparts, rows of small barred windows on
    the open faces, a parapet round the roof, and the round rubble tower
    on its north corner seen from the avenue (street photos 2019)."""
    poly = orient(poly, 1.0)
    stone = np.array(hexrgb(WALL_RUBBLE), float) * 0.98
    pale = np.array(hexrgb("#e2ddd2"), float)
    dark = np.array(hexrgb("#2a2826"), float)
    x0, y0, x1, y1 = poly.bounds
    TOP = 48.6
    P = np.asarray(poly.exterior.coords)
    n_win = 0
    for a, b in zip(P[:-1], P[1:]):
        L = float(np.hypot(*(b - a)))
        if L < 0.6:
            continue
        F = Frame(m, a, b - a)                     # t out of the building (outline counter-clockwise)
        ss = np.linspace(0.3, max(0.31, L - 0.3), max(2, int(L / 2)))
        g = mntR.sample(*np.array([F.xy(s_, 1.2) for s_ in ss]).T)
        gmin = float(np.nanmin(g)) if np.isfinite(g).any() else TOP - 3
        z0 = min(gmin, TOP) - 0.6
        F.quad((0.0, 0.0, z0), (L, 0.0, z0), (L, 0.0, TOP + 1.0), (0.0, 0.0, TOP + 1.0), stone, (0, 1, 0),
               style=RUBBLE, uvm=0.45)
        F.box(-0.05, L + 0.05, -0.45, 0.06, TOP + 1.0, TOP + 1.12, pale)               # coping
        F.quad((0.0, -0.45, TOP), (L, -0.45, TOP), (L, -0.45, TOP + 1.0), (0.0, -0.45, TOP + 1.0), stone, (0, -1, 0))
        if L < 3.5:
            continue
        nb = int(L / 3.2)
        for i, z_ in enumerate(np.arange(TOP - 3.2, gmin + 2.0, -3.3)):
            for k in range(nb):
                s_ = (k + 0.5) * L / nb
                gz = mntR.sample(*np.array([F.xy(s_, 1.2)]).T)[0]
                if np.isfinite(gz) and z_ < gz + 1.0:
                    continue
                F.window("+t", 0.0, s_, 0.8, z_, z_ + 1.15, k, i, frame=pale, sill=True)
                for d in (-0.2, 0.0, 0.2):                                              # bars
                    F.box(s_ + d - 0.025, s_ + d + 0.025, 0.02, 0.07, z_, z_ + 1.15, dark, top=False, sides="T")
                n_win += 1
    add_roof(m, poly, TOP, np.array(hexrgb("#cfc8b8"), float), False)
    # The round tower on the north corner.
    c = min(P[:-1], key=lambda q: np.hypot(q[0] - 1056572.0, q[1] - 6302544.0))
    gz = mntR.sample(np.array([c[0]]), np.array([c[1]]))[0]
    zt0 = (float(gz) if np.isfinite(gz) else TOP) - 4.0
    Ft = Frame(m, c, (1.0, 0.0))
    Ft.drum(0.0, 0.0, zt0, TOP + 5.5, 2.7, stone, 16, style=RUBBLE, uvm=0.45)
    Ft.cone(0.0, 0.0, TOP + 5.5, 2.75, 0.25, pale, 16, kind=PLAIN)
    for k, th in enumerate((0.6, 2.2, 3.8)):
        q = c + 2.72 * np.array([math.cos(th), math.sin(th)])
        Fw = Frame(m, q, (-math.sin(th), math.cos(th)))
        Fw.box(-0.35, 0.35, 0.0, 0.05, TOP + 2.6, TOP + 3.6, dark, top=False, sides="T")
    stats["prison"] = {"roof": TOP, "windows": n_win}
    return n_win


# ------------------------------------------------ the Palais de Justice ----
# Rue Colonel Bellando de Castro, 1922-1930, neo-Florentine in warm sandy
# ashlar. Its south end is one big round apse (the LiDAR's eaves fit a
# circle of 11.6 m tangent to both long walls; the walls stand ~0.8 m in),
# with LEX on the frieze under deep tiled eaves. The entrance is on that
# curve: a horseshoe double stair (OpenStreetMap maps both flights and the
# landing, the LiDAR sees them) whose flights start side by side in front
# of the ground-floor door, run outward and curl back to a balustraded
# landing; the upper door has a gabled stone porch on two columns. Photos:
# Mapillary 492163795170012 and 523753175670204 (2019, from the
# Conseil d'Etat corner), 888590281708010 (east side).
JUSTICE = "BATIMENT0000000074078969"
PJ_C = (1056180.65, 6302335.15)             # the apse's centre (eave circle from the LiDAR)
PJ_R = 10.75                                # m, its wall
PJ_NW = [(1056165.6, 6302353.1), (1056158.8, 6302352.9), (1056158.5, 6302345.9)]   # BD TOPO's north end
PJ_NE = (1056191.9, 6302354.4)
PJ_AXIS = -71.0                             # degrees (maths angle round PJ_C): the stair's axis, SSE
PJ_STONE = "#ddcbb0"                        # sandy ashlar (photos)
PJ_OCHRE = "#cdb084"                        # the stair, porch and carvings: warmer, yellower stone
PJ_EAVE = 73.6                              # m NGF (LiDAR roof 75-76 with 22 degree tiles)
PJ_LAND = 3.9                               # m: the landing above the forecourt
PJ_GF = 4.7                                 # m: the rusticated ground floor


def pj_outline():
    """The Palais de Justice's plan: BD TOPO's straight walls, its south
    end the round apse (centre PJ_C), counter-clockwise."""
    C = np.array(PJ_C)
    a, b = np.array(PJ_NW[2]), np.array(PJ_C)
    # The west wall: from the north-west corner toward the apse; its
    # tangent point is the foot of the centre on that line.
    d = np.array([19.0, -24.0]) / math.hypot(19.0, -24.0)
    tw = a + d * float(np.dot(C - a, d))
    th0 = math.atan2(*(tw - C)[::-1])
    r0 = float(np.hypot(*(tw - C)))
    ths = np.linspace(th0, 0.0, 34)
    rs = np.linspace(r0, PJ_R, 34)
    arc = [tuple(C + r * np.array([math.cos(t), math.sin(t)])) for r, t in zip(rs, ths)]
    pts = PJ_NW + arc + [(C[0] + PJ_R, PJ_NE[1] - 0.6), PJ_NE]
    return orient(Polygon(pts), 1.0), th0


def _bez(P, u):
    """A cubic Bezier (4 control points, (k, 2)) and its unit tangent at u."""
    P = np.asarray(P, float)
    u = np.atleast_1d(u)[:, None]
    q = ((1 - u) ** 3) * P[0] + 3 * ((1 - u) ** 2) * u * P[1] + 3 * (1 - u) * u * u * P[2] + u ** 3 * P[3]
    d = 3 * ((1 - u) ** 2) * (P[1] - P[0]) + 6 * (1 - u) * u * (P[2] - P[1]) + 3 * u * u * (P[3] - P[2])
    return q, d / np.linalg.norm(d, axis=1, keepdims=True)


def pj_flight(F, sg, zb, stone, ochre, n=24, w=1.5):
    """One flight of the horseshoe stair, in the stair's frame F (s along
    the landing, t out): from the forecourt beside the lower door (s = 2 sg)
    out and round to the landing's end, n solid steps between two curving
    parapets."""
    P = [(2.0 * sg, 6.4), (4.3 * sg, 6.7), (5.7 * sg, 5.1), (4.6 * sg, 2.75)]
    rise = PJ_LAND / n
    u = np.linspace(0.0, 1.0, n + 1)
    q, d = _bez(P, u)
    nrm = np.column_stack([-d[:, 1], d[:, 0]])           # left of the way up
    zg = zb - 0.4
    for k in range(n):
        z1 = zb + (k + 1) * rise
        L0, L1 = q[k] + nrm[k] * w / 2, q[k + 1] + nrm[k + 1] * w / 2
        R0, R1 = q[k] - nrm[k] * w / 2, q[k + 1] - nrm[k + 1] * w / 2
        up = (0, 0, 1)
        F.quad((*L0, z1), (*R0, z1), (*R1, z1), (*L1, z1), ochre * 1.04, up)
        F.quad((*L0, z1 - rise), (*R0, z1 - rise), (*R0, z1), (*L0, z1), ochre * 0.86, (-d[k][0], -d[k][1], 0))
        # The two parapets: solid stone from the ground, their tops
        # sloping with the flight; inner faces from the tread up.
        for side in (1.0, -1.0):
            o0, o1 = q[k] + side * nrm[k] * (w / 2 + 0.24), q[k + 1] + side * nrm[k + 1] * (w / 2 + 0.24)
            i0, i1 = q[k] + side * nrm[k] * w / 2, q[k + 1] + side * nrm[k + 1] * w / 2
            zt0, zt1 = zb + k * rise + 1.0, zb + (k + 1) * rise + 1.0
            nn = (side * nrm[k][0], side * nrm[k][1], 0)
            F.quad((*o0, zg), (*o1, zg), (*o1, zt1), (*o0, zt0), stone, nn, style=RUBBLE, uvm=0.9)
            F.quad((*i0, z1), (*i1, z1), (*i1, zt1), (*i0, zt0), ochre * 0.95, (-nn[0], -nn[1], 0))
            F.quad((*i0, zt0), (*i1, zt1), (*o1, zt1 + 0.03), (*o0, zt0 + 0.03), ochre * 1.08, (0, 0, 1))
    # The bottom: a riser to the ground, carved newel pedestals with a
    # scroll on each parapet's foot.
    for side in (1.0, -1.0):
        c = q[0] + side * nrm[0] * (w / 2 + 0.12)
        Fn = Frame(F.m, F.xy(*c), F.u)
        Fn.box(-0.45, 0.45, -0.45, 0.45, zg, zb + 1.45, ochre)
        Fn.box(-0.55, 0.55, -0.55, 0.55, zb + 1.45, zb + 1.62, ochre * 1.06)
        Fn.box(-0.32, 0.32, -0.32, 0.32, zb + 1.62, zb + 1.95, ochre * 1.02)
    # The flight's underside where it leaves the ground: closed by its
    # outer parapet faces; the end walls at the bottom.
    for k in (0,):
        L0, R0 = q[k] + nrm[k] * (w / 2 + 0.24), q[k] - nrm[k] * (w / 2 + 0.24)
        F.quad((*L0, zg), (*R0, zg), (*R0, zb + 1.0), (*L0, zb + 1.0), stone, (-d[k][0], -d[k][1], 0))
    return n


def palais_justice(m, mntR, stats):
    """The Palais de Justice: rusticated ground floor, sandy ashlar above,
    paired arched windows, a frieze and deep tiled eaves, the round apse on
    the south with the horseshoe entrance stair. Returns its plan."""
    poly, th0 = pj_outline()
    stone = np.array(hexrgb(PJ_STONE), float)
    ochre = np.array(hexrgb(PJ_OCHRE), float)
    wood = np.array(hexrgb("#6a4529"), float)
    C = np.array(PJ_C)
    P = np.asarray(poly.exterior.coords)
    gz = mntR.sample(P[:, 0], P[:, 1])
    base = float(np.nanmin(gz)) if np.isfinite(gz).any() else 60.6
    # The forecourt in front of the stair.
    ax = math.radians(PJ_AXIS)
    v = np.array([math.cos(ax), math.sin(ax)])
    O = C + PJ_R * v
    F = Frame(m, O, (-v[1], v[0]))                      # s along the landing (west to east), t out
    fz = mntR.sample(*np.array([F.xy(0.0, 4.5)]).T)[0]
    zb = float(fz) if np.isfinite(fz) else 60.75
    zl = zb + PJ_LAND
    zg1 = zb + PJ_GF                                    # top of the rusticated floor
    n_win = 0
    # Walls, facet by facet: rusticated to zg1, ashlar to the eaves.
    for a, b in zip(P[:-1], P[1:]):
        L = float(np.hypot(*(b - a)))
        if L < 0.05:
            continue
        Fw = Frame(m, a, b - a)
        g = mntR.sample(*np.array([Fw.xy(s_, 1.0) for s_ in np.linspace(0, L, 3)]).T)
        z0 = (float(np.nanmin(g)) if np.isfinite(g).any() else base) - 0.6
        Fw.quad((0.0, 0.0, z0), (L, 0.0, z0), (L, 0.0, zg1), (0.0, 0.0, zg1), stone * 0.95, (0, 1, 0),
                style=RUBBLE, uvm=0.5)
        Fw.quad((0.0, 0.0, zg1), (L, 0.0, zg1), (L, 0.0, PJ_EAVE), (0.0, 0.0, PJ_EAVE), stone, (0, 1, 0),
                style=RUBBLE, uvm=0.95)
        Fw.box(-0.02, L + 0.02, 0.0, 0.22, zg1 - 0.05, zg1 + 0.3, ochre * 1.05, sides="tT")       # string course
        Fw.box(-0.02, L + 0.02, 0.0, 0.14, PJ_EAVE - 1.5, PJ_EAVE - 1.3, ochre * 1.05, sides="tT")   # frieze
        Fw.box(-0.02, L + 0.02, 0.0, 0.3, PJ_EAVE - 0.35, PJ_EAVE, stone * 1.06, sides="tT", bottom=True)
    # Windows: arched, in pairs on the upper floors, one per bay below.
    # Bays along the straight walls every 3.6 m; on the apse every 20
    # degrees either side of the stair's axis.
    bays = []
    straight = [(np.array(a), np.array(b)) for a, b in zip(P[:-1], P[1:]) if np.hypot(*(np.array(b) - a)) > 6]
    for a, b in straight:
        L = float(np.hypot(*(b - a)))
        nb = int((L - 2.0) / 3.6)
        for k in range(nb):
            s_ = (L - (nb - 1) * 3.6) / 2 + k * 3.6
            bays.append((Frame(m, a, b - a), s_, False))
    for k in range(-4, 4):
        th = PJ_AXIS + 20.0 * k
        if not (math.degrees(th0) + 6 < th < -6):
            continue
        t_ = math.radians(th)
        o = C + PJ_R * np.array([math.cos(t_), math.sin(t_)])
        bays.append((Frame(m, o - 0.5 * np.array([-math.sin(t_), math.cos(t_)]), (-math.sin(t_), math.cos(t_))),
                     0.5, k == 0))
    for i, (Fb, s_, axis) in enumerate(bays):
        on_stair = abs((Fb.u @ np.array([-v[1], v[0]]))) > 0.75 and np.hypot(*(Fb.xy(s_, 0) - O)) < 6.5
        c = 0.03
        # Ground floor: one round-headed window (behind the stair: none).
        if not on_stair:
            Fb.window("+t", c, s_, 1.0, zb + 1.3, zb + 3.0, i, 0, frame=ochre, arch=True, sill=True)
            n_win += 1
        # First floor: a pair (the stair's axis: the upper door instead).
        if not axis:
            for ds in (-0.62, 0.62):
                Fb.window("+t", c, s_ + ds, 0.9, zl + 1.3, zl + 3.4, i, 1, frame=ochre, arch=True, sill=True,
                          band=0.16)
            Fb.slab("+t", c, s_ - 0.08, s_ + 0.08, zl + 1.3, zl + 3.4, 0.14, ochre)          # the mullion
            n_win += 2
        # Second floor: a pair under one relieving arch.
        for ds in (-0.55, 0.55):
            Fb.window("+t", c, s_ + ds, 0.8, zl + 4.7, zl + 6.1, i, 2, frame=ochre, arch=True, sill=True, band=0.14)
        Fb.arch_ring("+t", c, s_, 2.3, zl + 6.1, 0.1, 0.22, ochre)
        n_win += 2
    # The landing: solid stone either side of the lower door's porch, a
    # lintel over the porch, the balustrade along its front and ends.
    WL, TL = 5.4, 2.8
    tb = -(PJ_R - math.sqrt(PJ_R ** 2 - WL ** 2)) - 0.3
    for s0, s1 in ((-WL, -1.45), (1.45, WL)):
        F.box(s0, s1, tb, TL, zb - 0.4, zl, stone * 0.96, style=RUBBLE, uvm=0.55)
    F.box(-1.45, 1.45, -0.3, TL, zl - 0.9, zl, stone * 0.96, bottom=True)
    F.box(-WL - 0.05, WL + 0.05, tb, TL + 0.08, zl - 0.12, zl, ochre * 1.04)               # landing floor edge
    F.box(-1.6, 1.6, TL, TL + 0.1, zl - 0.95, zl - 0.7, ochre * 1.06)                      # lintel moulding
    for sg in (-1, 1):
        F.column(sg * 1.62, TL - 0.32, zb, zl - 1.05, 0.27, ochre)
    F.quad((-1.45, 0.0, zb + 0.02), (1.45, 0.0, zb + 0.02), (1.45, TL, zb + 0.02), (-1.45, TL, zb + 0.02),
           ochre * 0.9, (0, 0, 1))
    # The lower door, in the wall at the back of the porch.
    F.box(-0.95, 0.95, -0.05, 0.08, zb, zb + 2.9, wood, sides="T")
    F.arch_glass("+t", 0.08, 0.0, 1.9, zb + 2.9, 0, 0, col="#4a3a2c", dark=True)
    F.arch_ring("+t", 0.0, 0.0, 1.9, zb + 2.9, 0.12, 0.25, ochre)
    F.balustrade("+t", TL + 0.05, -3.85, 3.85, zl, ochre, h=1.0)
    for sg in (-1, 1):
        F.box(sg * 3.85 - 0.25, sg * 3.85 + 0.25, TL - 0.45, TL + 0.05, zl, zl + 1.15, ochre * 1.02)   # end posts
        F.balustrade("+s" if sg > 0 else "-s", sg * WL, tb + 0.3, TL - 0.45, zl, ochre, h=1.0)
    # The upper door and its gabled porch on two columns.
    F.box(-0.95, 0.95, -0.05, 0.1, zl, zl + 2.9, wood, sides="T")
    F.arch_glass("+t", 0.1, 0.0, 1.9, zl + 2.9, 0, 1, col="#3c3a36")
    zc = zl + 4.0
    for sg in (-1, 1):
        F.column(sg * 1.35, 1.05, zl, zc, 0.19, ochre)
        F.box(sg * 1.35 - 0.3, sg * 1.35 + 0.3, -0.1, 0.3, zl, zc, ochre * 0.98)      # pilasters on the wall
    F.box(-1.75, 1.75, -0.1, 1.4, zc, zc + 0.45, ochre * 1.05, bottom=True)
    F.arch_ring("+t", 1.25, 0.0, 2.2, zc - 1.1, 0.15, 0.2, ochre * 0.97)
    rise = 1.35
    for sg in (-1, 1):
        F.quad((0.0, -0.1, zc + 0.45 + rise), (0.0, 1.55, zc + 0.45 + rise), (sg * 1.95, 1.55, zc + 0.3),
               (sg * 1.95, -0.1, zc + 0.3), ochre * 0.92, (sg * rise, 0, 1.9))
    F.tri((-1.75, 1.4, zc + 0.45), (1.75, 1.4, zc + 0.45), (0.0, 1.4, zc + 0.45 + rise - 0.12), ochre * 1.02,
          (0, 1, 0))
    F.box(-0.3, 0.3, 1.38, 1.5, zc + 0.6, zc + 1.2, stone * 1.1)                         # the shield
    # The two flights.
    steps = pj_flight(F, -1, zb, stone, ochre) + pj_flight(F, 1, zb, stone, ochre)
    # The eaves: deep, on brackets, the tiled hip roof (a low cap of
    # 22 degree slopes round the light well, which stays a flat glazed roof).
    over = poly.buffer(1.1, join_style=2, mitre_limit=3.0)
    slope = math.tan(math.radians(22))
    t = skeleton_roof(orient(over, 1.0), PJ_EAVE - 1.1 * slope, slope, 7.0, dt=0.4)
    if t is not None:
        vv, zz, tri = t
        Q = enu_fast(vv[:, 0], vv[:, 1], zz)
        m.tri(Q[tri[:, 0]], Q[tri[:, 1]], Q[tri[:, 2]], np.array(hexrgb("#b86545"), float), ROOF_TILE)
    well = over.buffer(-11.0, join_style=2)
    if not well.is_empty:
        add_roof(m, orient(max(to_polys(well), key=lambda q: q.area), 1.0), PJ_EAVE - 1.1 * slope + 7.0 * slope + 0.02,
                 np.array(hexrgb("#9a968e"), float), False)
    # Soffit and brackets under the overhang.
    Ps = np.asarray(over.exterior.coords)
    for a, b in zip(Ps[:-1], Ps[1:]):
        L = float(np.hypot(*(b - a)))
        if L < 0.05:
            continue
        Fo = Frame(m, a, b - a)
        Fo.quad((0.0, 0.0, PJ_EAVE - 1.1 * slope - 0.12), (L, 0.0, PJ_EAVE - 1.1 * slope - 0.12),
                (L, -1.15, PJ_EAVE - 0.08), (0.0, -1.15, PJ_EAVE - 0.08), np.array(hexrgb("#a07a58"), float),
                (0, 0, -1))
        Fo.quad((0.0, 0.0, PJ_EAVE - 1.1 * slope - 0.12), (L, 0.0, PJ_EAVE - 1.1 * slope - 0.12),
                (L, 0.0, PJ_EAVE - 1.1 * slope), (0.0, 0.0, PJ_EAVE - 1.1 * slope), np.array(hexrgb("#a07a58"), float),
                (0, 1, 0))
        for k in range(int(L / 0.9)):
            s_ = (k + 0.5) * L / int(L / 0.9)
            Fo.box(s_ - 0.07, s_ + 0.07, -1.1, -0.05, PJ_EAVE - 0.4, PJ_EAVE - 0.12, np.array(hexrgb("#8a6748"), float),
                   bottom=True)
    stats["palais_justice"] = {"windows": n_win, "steps": steps, "landing": round(zl, 2), "eave": PJ_EAVE}
    return poly


def rocher_ramparts(m, ways, pts, mnt, stats):
    """The Rock's ramparts: the mapped retaining walls in pale limestone
    rubble with a parapet on the high side, and the battery of bronze
    cannons along the west parapet (OpenStreetMap historic=cannon, with
    their directions)."""
    mntR = Raster(mnt)
    zone = ROCHER_ZONE.buffer(60.0)
    st = np.array(hexrgb(FORT_STONE), float)
    n_m = 0.0
    for t, p_, closed in ways:
        if t.get("barrier") != "retaining_wall" or len(p_) < 2 or not LineString(p_).intersects(zone):
            continue
        P = np.asarray(p_, float)
        for a, b in zip(P[:-1], P[1:]):
            L = float(np.hypot(*(b - a)))
            if L < 0.5:
                continue
            F = Frame(m, a, b - a)
            ss = np.linspace(0.0, L, max(2, int(L / 2) + 1))
            hi = mntR.sample(*np.array([F.xy(s_, 1.6) for s_ in ss]).T)
            lo = mntR.sample(*np.array([F.xy(s_, -1.6) for s_ in ss]).T)
            if not (np.isfinite(hi).any() and np.isfinite(lo).any()):
                continue
            hi_, lo_ = float(np.nanmax(hi)), float(np.nanmin(lo))
            side = 1.0
            if lo_ > hi_ or float(np.nanmedian(lo)) > float(np.nanmedian(hi)):
                hi_, lo_ = float(np.nanmax(lo)), float(np.nanmin(hi))
                side = -1.0
            if hi_ - lo_ < 0.8:
                continue                                   # no drop: a low garden wall, left to the ground
            F.box(0.0, L, -0.3, 0.3, lo_ - 0.5, hi_ + 1.0, st, style=RUBBLE, uvm=0.45)
            F.box(-0.05, L + 0.05, -0.36, 0.36, hi_ + 1.0, hi_ + 1.15, st * 1.06)       # coping
            n_m += L
    n_c = 0
    for (x, y), t in pts:
        if t.get("historic") == "cannon" and zone.contains(Point(x, y)):
            z = float(np.nan_to_num(mntR.sample(np.array([x]), np.array([y]))[0], nan=60.0))
            try:
                deg = float(t.get("direction", "0"))
            except ValueError:
                deg = 0.0
            _cannon(m, x, y, z, deg)
            n_c += 1
    stats["rampart_m"] = round(n_m)
    stats["rampart_cannons"] = n_c
    return round(n_m)


def rocher_gardens_setup(ways):
    near = ROCHER_ZONE.buffer(100.0)
    g = [Polygon(p_).buffer(0) for t, p_, c in ways
         if c and len(p_) > 3 and t.get("leisure") in ("garden", "park") and Polygon(p_).buffer(0).intersects(near)]
    return unary_union(g) if g else None


def rocher_gardens(gm, m, ways, pts, mnt, gz, stats):
    """The Rock's gardens (Jardins de Saint-Martin and the small ones by
    the Palace): their mapped paths in red brick between the pines, and
    green benches where OpenStreetMap has them. The trees come from the
    LiDAR (trees()), the statues from street_features()."""
    mntR = Raster(mnt)
    if ROCHER_GARDENS is None:
        return 0
    gardens = ROCHER_GARDENS
    paths = [LineString(p_) for t, p_, c in ways
             if t.get("highway") in ("footway", "path", "steps", "pedestrian") and not c and len(p_) >= 2
             and LineString(p_).intersects(gardens)]
    pv = unary_union([ln.buffer(0.95, cap_style=1) for ln in paths] or [Polygon()]).intersection(gardens)
    stats["rocher_garden_path_m2"] = round(_slab(gm, pv, mntR, gz, PAVER, lift=0.08, area=1.5))
    wood, iron = np.array(hexrgb("#33463c")), np.array(hexrgb("#2c2a27"))
    nb = 0
    for (x, y), t in pts:
        if t.get("amenity") == "bench" and gardens.buffer(3).contains(Point(x, y)):
            z = mntR.sample(np.array([x]), np.array([y]))[0]
            if not np.isfinite(z):
                continue
            # Facing the nearest path.
            ax = (1.0, 0.0)
            if paths:
                q = min(paths, key=lambda ln: ln.distance(Point(x, y)))
                pp = q.interpolate(q.project(Point(x, y)))
                d = np.array([pp.x - x, pp.y - y])
                if np.hypot(*d) > 0.2:
                    ax = (-d[1], d[0])
            _box(m, x, y, ax, 1.8, 0.5, z + 0.4, z + 0.47, wood)
            _box(m, x, y, ax, 1.7, 0.4, z - 0.1, z + 0.4, iron)
            nb += 1
    stats["rocher_benches"] = nb
    return nb


def _zone_trees(m, zone, mns, mnt, ortho, fps, kindfn, hmax=24.0, rad_k=0.85, seed=31):
    """Trees where the LiDAR canopy peaks inside zone (buildings near it cut
    out; red awnings and canopies are not trees). kindfn(h) names the kind."""
    zone = zone.difference(unary_union([p for p in fps if p.distance(zone) < 15] or [Polygon()]))
    if zone.is_empty:
        return 0
    x0, y0, x1, y1 = zone.bounds
    c0, c1 = int((x0 - X0) / RES), int((x1 - X0) / RES)
    r0, r1 = int((Y1 - y1) / RES), int((Y1 - y0) / RES)
    chm = np.nan_to_num(mns[r0:r1, c0:c1] - mnt[r0:r1, c0:c1])
    o = ortho[r0:r1, c0:c1].astype(np.float32)
    red = (o[..., 0] - o[..., 1] > 40) & (chm < 5)
    gx, gy = cell_xy(np.arange(r0, r1)[:, None], np.arange(c0, c1)[None, :])
    gx, gy = np.broadcast_arrays(gx, gy)
    inz = contains_xy(zone, gx, gy)
    sm = ndimage.gaussian_filter(np.where(inz & ~red, chm, 0), 1.5)
    mx = ndimage.maximum_filter(sm, size=9)
    rnd = np.random.default_rng(seed)
    n_ = 0
    for r, c in np.argwhere((sm == mx) & (sm > 3.2) & inz):
        h = float(min(chm[max(0, r - 2):r + 3, max(0, c - 2):c + 3].max(), 24))
        win = chm[max(0, r - 14):r + 15, max(0, c - 14):c + 15] > max(2.5, h * 0.45)
        rad = float(np.clip(math.sqrt(win.sum() * RES * RES / math.pi), 1.4, 6.5))
        x, y = gx[r, c], gy[r, c]
        z = float(mnt[r0 + r, c0 + c])
        if not np.isfinite(z):
            continue
        kind = kindfn(h)
        add_tree(m, x, y, z, min(h, hmax), rad * (0.9 if kind == "pine" else rad_k), kind, rnd)
        n_ += 1
    return n_


def rocher_trees(m, mns, mnt, ortho, fps, stats):
    """The umbrella pines on Place du Palais and the trees round it, from
    the LiDAR canopy (the photo is blurred there, so the generic finder,
    which wants green in it, misses them)."""
    # Pines only on the south side of the square (by the cannons and down
    # to the ramparts); the square in front of the gate and the towers is
    # open paving.
    zone = Frame(None, PALACE_O, PALACE_U).poly(-60.0, 8.0, 0.5, 70.0)
    n_ = _zone_trees(m, zone, mns, mnt, ortho, fps, lambda h: "pine" if h > 7 else "broad", seed=47)
    # The gardens (Saint-Martin's pine wood on the south cliff), from the
    # canopy too: the generic finder thinned them to a few cypresses.
    if ROCHER_GARDENS is not None:
        n_ += _zone_trees(m, ROCHER_GARDENS.difference(zone), mns, mnt, ortho, fps,
                          lambda h: "pine" if h > 6 else "broad", seed=53)
    stats["rocher_trees"] = n_
    return n_


def armes_trees(m, mns, mnt, ortho, fps, stats):
    """The square's trees where the LiDAR has them (the generic tree finder
    wants green in the photo and missed the tall dark pines): tall ones are
    the pines along the road and by the market hall, low ones small leafy
    trees. The market's red canopies are not trees."""
    n_ = _zone_trees(m, ARMES["zone"], mns, mnt, ortho, fps, lambda h: "pine" if h > 11 else "broad")
    stats["armes_trees"] = n_
    return n_


def _barrel(m, c, u, L, W, z, rise, col, nseg=10):
    """A barrel roof along u, L long, W wide, springing at z."""
    v = np.array([-u[1], u[0]])
    t = np.linspace(0, math.pi, nseg + 1)
    xs, zs = np.cos(t) * W / 2, z + np.sin(t) * rise
    a, b = c - u * L / 2, c + u * L / 2
    for i in range(nseg):
        p = [a + v * xs[i], a + v * xs[i + 1], b + v * xs[i + 1], b + v * xs[i]]
        zz = [zs[i], zs[i + 1], zs[i + 1], zs[i]]
        P = enu_fast(np.array([q[0] for q in p]), np.array([q[1] for q in p]), np.array(zz))
        _quad(m, P[0][None], P[1][None], P[2][None], P[3][None], col * (0.92 + 0.08 * math.sin(t[i])),
              np.array([[0.0, 0.0, 1.0]]))
    for e in (a, b):   # the ends
        ring = np.array([e + v * x_ for x_ in xs])
        P = enu_fast(ring[:, 0], ring[:, 1], zs)
        cen = enu_fast(np.array([e[0]]), np.array([e[1]]), np.array([z]))[0]
        for i in range(nseg):
            for A, B_ in ((P[i], P[i + 1]), (P[i + 1], P[i])):
                m.tri(cen[None], A[None], B_[None], col * 0.8, PLAIN)


def armes_furniture(m, ways, pts, mntR, ortho_hi, stats):
    """Kiosks, playground, market stalls, lamps and bollards."""
    sq = ARMES["poly"]
    u_, v_ = ARMES["u"], ARMES["v"]
    zat = lambda x, y: float(np.nan_to_num(mntR.sample(np.array([x]), np.array([y]))[0], nan=19.5))
    wood, roofc = np.array(hexrgb(ARMES_KIOSK)), np.array(hexrgb(ARMES_KIOSK_ROOF))
    iron = np.array(hexrgb(ARMES_IRON))
    n_k = 0
    for t, p, closed in ways:
        if not closed or len(p) < 4:
            continue
        g = Polygon(p).buffer(0)
        if g.is_empty or g.distance(sq) > 2 or g.area > 150:
            continue
        rr = np.asarray(g.minimum_rotated_rectangle.exterior.coords)
        e0, e1 = rr[1] - rr[0], rr[2] - rr[1]
        L, W = float(np.linalg.norm(e0)), float(np.linalg.norm(e1))
        u = e0 / L
        if W > L:
            u, L, W = e1 / W, W, L
        c = np.array([g.centroid.x, g.centroid.y])
        z = zat(*c)
        if t.get("building") == "kiosk":
            # The snack bar: dark varnished wood and glass, a white awning
            # on the square side, three brown barrel roofs.
            body = orient(g.buffer(-0.15, join_style=2), 1.0)
            add_walls(m, body, z - 0.3, z + 0.9, z, 10, wood, PLAIN)
            ring = np.asarray(body.exterior.coords)
            for a, b in zip(ring[:-1], ring[1:]):
                Le = float(np.linalg.norm(b - a))
                if Le < 0.8:
                    continue
                ue = (b - a) / Le
                out = np.array([ue[1], -ue[0]])
                _vpanel(m, (a + b) / 2 + out * 0.02, ue, -out,
                        [(-Le / 2 + 0.2, z + 0.9), (Le / 2 - 0.2, z + 0.9), (Le / 2 - 0.2, z + 2.5), (-Le / 2 + 0.2, z + 2.5)],
                        np.array(hexrgb("#d9b27a")), depth=0.02, style=LIT_GLASS)
                k = max(1, int(round(Le / 1.2)))
                for i in range(k + 1):
                    q = a + ue * Le * i / k + out * 0.05
                    _box(m, q[0], q[1], ue, 0.12, 0.12, z + 0.9, z + 2.5, wood)
            add_walls(m, orient(g.buffer(0.1, join_style=2), 1.0), z + 2.5, z + 2.9, z, 10, wood, PLAIN)
            add_roof(m, orient(g.buffer(0.1, join_style=2), 1.0), z + 2.9, wood * 0.8, False)
            nb = 3
            for i in range(nb):
                cc = c + u * (L * ((i + 0.5) / nb - 0.5))
                _barrel(m, cc, u, L / nb - 0.15, W + 0.5, z + 2.9, 1.1, roofc)
            # Awning: the long side facing the square's middle.
            n_side = np.array([-u[1], u[0]])
            if n_side @ (np.array([sq.centroid.x, sq.centroid.y]) - c) < 0:
                n_side = -n_side
            a0 = c + n_side * (W / 2 + 0.1)
            P = [a0 - u * L / 2, a0 + u * L / 2, a0 + u * L / 2 + n_side * 2.4, a0 - u * L / 2 + n_side * 2.4]
            P = enu_fast(np.array([q[0] for q in P]), np.array([q[1] for q in P]),
                         np.array([z + 2.5, z + 2.5, z + 2.15, z + 2.15]))
            _quad(m, P[0][None], P[1][None], P[2][None], P[3][None], np.array(hexrgb("#efebe2")), np.array([[0.0, 0.0, 1.0]]))
            _quad(m, P[0][None], P[1][None], P[2][None], P[3][None], np.array(hexrgb("#d8d2c6")), np.array([[0.0, 0.0, -1.0]]))
            n_k += 1
        elif t.get("building") == "yes" and g.area < 30:
            # The newspaper kiosk: dark brown slats, a red-edged flat roof.
            add_walls(m, orient(g, 1.0), z - 0.3, z + 2.6, z, 10, wood * 1.1, PLAIN)
            cap = orient(g.buffer(0.35, join_style=2), 1.0)
            add_walls(m, cap, z + 2.6, z + 2.95, z, 10, np.array(hexrgb("#a8302a")), PLAIN)
            add_roof(m, cap, z + 2.95, wood, False)
            n_k += 1
        elif t.get("leisure") == "playground":
            # A low wooden fence round it, a play tower with red panels
            # under a slate-blue roof, a slide.
            ring = np.asarray(orient(g, 1.0).exterior.coords)
            fence = np.array(hexrgb("#a9773f"))
            for a, b in zip(ring[:-1], ring[1:]):
                Le = float(np.linalg.norm(b - a))
                ue = (b - a) / max(Le, 1e-6)
                k = max(1, int(Le / 0.25))
                for i in range(k + 1):
                    q = a + ue * Le * i / k
                    _box(m, q[0], q[1], ue, 0.08, 0.04, z, z + 0.95, fence)
                for zz in (z + 0.3, z + 0.8):
                    _box(m, *((a + b) / 2), ue, Le, 0.05, zz, zz + 0.1, fence * 0.85)
            tc = c + u * (L * 0.2)
            for sx in (-1, 1):
                for sy in (-1, 1):
                    q = tc + u * sx * 0.9 + np.array([-u[1], u[0]]) * sy * 0.9
                    _box(m, q[0], q[1], u, 0.14, 0.14, z, z + 3.6, fence)
            _box(m, tc[0], tc[1], u, 2.0, 2.0, z + 1.4, z + 1.55, fence)
            for sy in (-1, 1):
                q = tc + np.array([-u[1], u[0]]) * sy * 0.95
                _box(m, q[0], q[1], u, 1.7, 0.05, z + 1.55, z + 2.4, np.array(hexrgb("#b52b26")))
            pyr = orient(Polygon([tc + u * 1.3 + np.array([-u[1], u[0]]) * 1.3, tc - u * 1.3 + np.array([-u[1], u[0]]) * 1.3,
                                  tc - u * 1.3 - np.array([-u[1], u[0]]) * 1.3, tc + u * 1.3 - np.array([-u[1], u[0]]) * 1.3]), 1.0)
            add_roof(m, pyr, z + 3.6, np.array(hexrgb("#3d4d5c")), True, 1.0)
            sa, sb = tc - u * 1.0, tc - u * 4.2
            P = [sa + np.array([-u[1], u[0]]) * 0.35, sa - np.array([-u[1], u[0]]) * 0.35,
                 sb - np.array([-u[1], u[0]]) * 0.35, sb + np.array([-u[1], u[0]]) * 0.35]
            P = enu_fast(np.array([q[0] for q in P]), np.array([q[1] for q in P]), np.array([z + 1.5, z + 1.5, z + 0.3, z + 0.3]))
            _quad(m, P[0][None], P[1][None], P[2][None], P[3][None], np.array(hexrgb("#d9c23a")), np.array([[0.0, 0.0, 1.0]]))
            n_k += 1
    stats["armes_kiosks"] = n_k

    # Market stalls where the 20 cm photo shows the red canopies, on the
    # square's grid (3 x 3 m stalls, in rows).
    n_s = 0
    if ortho_hi is not None:
        hi = ortho_hi
        W_ = hi.size[0]
        pxm = (X1 - X0) / W_
        x0, y0, x1, y1 = sq.bounds
        crop = np.asarray(hi.crop((int((x0 - X0) / pxm), int((Y1 - y1) / pxm),
                                   int((x1 - X0) / pxm) + 1, int((Y1 - y0) / pxm) + 1)).convert("RGB")).astype(np.float32)
        redm = (crop[..., 0] - crop[..., 1] > 55) & (crop[..., 0] > 130) & (crop[..., 0] - crop[..., 2] > 45)
        ys, xs = np.nonzero(redm)
        X = x0 + (xs + 0.5) * pxm
        Y = y1 - (ys + 0.5) * pxm
        G = armes_grid(X, Y)
        S = 3.0
        cells = {}
        for k_ in zip(np.floor(G[:, 0] / S).astype(int), np.floor(G[:, 1] / S).astype(int)):
            cells[k_] = cells.get(k_, 0) + 1
        need = 0.35 * (S / pxm) ** 2
        canopy = np.array(hexrgb(ARMES_CANOPY))
        white = np.array(hexrgb("#ece8df"))
        for (i, j), cnt in cells.items():
            if cnt < need:
                continue
            s, tt = (i + 0.5) * S + ARMES_PHASE[0], (j + 0.5) * S + ARMES_PHASE[1]
            c = ARMES["o"] + u_ * s + v_ * tt
            if not sq.buffer(-0.5).contains(Point(*c)):
                continue
            z = zat(*c)
            for sx in (-1, 1):
                for sy in (-1, 1):
                    q = c + u_ * sx * 1.35 + v_ * sy * 1.35
                    _box(m, q[0], q[1], u_, 0.06, 0.06, z, z + 2.3, white * 0.8)
            _box(m, *(c - v_ * 0.6), u_, 2.6, 0.9, z, z + 0.9, white)          # the counter
            # Crates of fruit, vegetables and flowers on the counter.
            for kk in range(4):
                q = c - v_ * 0.6 + u_ * (kk - 1.5) * 0.62
                _box(m, q[0], q[1], u_, 0.55, 0.7, z + 0.9, z + 1.02,
                     np.array(hexrgb(["#d9822b", "#6f9a3a", "#c8402e", "#e3c34a", "#8e5a9c", "#a7c76a"][(i * 7 + j * 3 + kk) % 6])))
            ridge = [c - u_ * 1.5, c + u_ * 1.5]
            for sy in (-1, 1):
                e = [c - u_ * 1.5 + v_ * sy * 1.5, c + u_ * 1.5 + v_ * sy * 1.5]
                P = enu_fast(np.array([e[0][0], e[1][0], ridge[1][0], ridge[0][0]]),
                             np.array([e[0][1], e[1][1], ridge[1][1], ridge[0][1]]),
                             np.array([z + 2.3, z + 2.3, z + 2.9, z + 2.9]))
                _quad(m, P[0][None], P[1][None], P[2][None], P[3][None], canopy, np.array([[0.0, 0.0, 1.0]]))
                _quad(m, P[0][None], P[1][None], P[2][None], P[3][None], canopy * 0.7, np.array([[0.0, 0.0, -1.0]]))
            for e0 in (c - u_ * 1.5, c + u_ * 1.5):      # gable ends
                P = enu_fast(np.array([(e0 - v_ * 1.5)[0], (e0 + v_ * 1.5)[0], e0[0]]),
                             np.array([(e0 - v_ * 1.5)[1], (e0 + v_ * 1.5)[1], e0[1]]), np.array([z + 2.3, z + 2.3, z + 2.9]))
                m.tri(P[0][None], P[1][None], P[2][None], canopy * 0.9, PLAIN)
                m.tri(P[0][None], P[2][None], P[1][None], canopy * 0.9, PLAIN)
            n_s += 1
    stats["armes_stalls"] = n_s

    # Lamps round the square: a tall dark brown cast-iron post with a
    # cluster of four lantern globes.
    ring = sq.exterior
    n_l = 0
    for s in np.arange(4.0, ring.length, 16.0):
        p = ring.interpolate(s)
        q = shapely.ops.nearest_points(sq.buffer(-1.2).exterior, p)[0]
        x, y = q.x, q.y
        z = zat(x, y)
        add_walls(m, box(x - 0.09, y - 0.09, x + 0.09, y + 0.09), z - 0.3, z + 4.6, z, 10, iron, PLAIN, min_edge=0.05)
        add_walls(m, box(x - 0.22, y - 0.22, x + 0.22, y + 0.22), z - 0.3, z + 1.0, z, 10, iron, PLAIN)
        add_roof(m, box(x - 0.22, y - 0.22, x + 0.22, y + 0.22), z + 1.0, iron, False)
        for ang in (0.0, math.pi / 2, math.pi, 1.5 * math.pi):
            hx, hy = x + 0.55 * math.cos(ang + 0.3), y + 0.55 * math.sin(ang + 0.3)
            arm = LineString([(x, y), (hx, hy)]).buffer(0.04, cap_style=2)
            add_walls(m, orient(arm, 1.0), z + 4.25, z + 4.33, z, 10, iron, PLAIN)
            _ball(m, hx, hy, z + 4.6, 0.2, np.array(hexrgb("#f4efe2")), LAMP_HEAD)
        _ball(m, x, y, z + 5.0, 0.22, np.array(hexrgb("#f4efe2")), LAMP_HEAD)
        n_l += 1
    stats["armes_lamps"] = n_l

    # Bollards along the square's edge on the road side.
    roads = unary_union([LineString(p).buffer(9.0) for t, p, c in ways
                         if t.get("highway") in ("secondary", "residential", "tertiary") and not c])
    n_b = 0
    edge = sq.buffer(-0.4).exterior
    for s in np.arange(0.0, edge.length, 1.9):
        p = edge.interpolate(s)
        if not roads.contains(p):
            continue
        z = zat(p.x, p.y)
        _box(m, p.x, p.y, u_, 0.2, 0.2, z, z + 0.85, iron)
        _box(m, p.x, p.y, u_, 0.22, 0.22, z + 0.85, z + 0.92, np.array(hexrgb("#c9455a")))
        n_b += 1
    stats["armes_bollards"] = n_b
    return n_s


def place_darmes(gm, m, tm, ways, pts, mns, mnt, ortho, ortho_hi, gz, fps, stats):
    if ARMES is None:
        return 0
    mntR = Raster(mnt)
    armes_paving(gm, mntR, gz, stats)
    armes_trees(tm, mns, mnt, ortho, fps, stats)
    return armes_furniture(m, ways, pts, mntR, ortho_hi, stats)


# La Condamine beyond the square. Square Théodore Gastaud (1923, redone in
# 2000 over a car park): salmon paving, the white "romantic" bandstand in
# the middle (the white octagon in the 20 cm photo, 5 m high in the LiDAR),
# olive trees pruned "Tuscan" style (Visit Monaco), the bronze "Le lion et
# la Sirène". The pedestrian streets (Rue Princesse Caroline, Rue de Millo,
# Rue Langlé, Rue Louis Notari...): pale limestone slabs with a strip of
# dark setts down the middle and bitter-orange trees in stone pits (street
# photos 2021). Rue Grimaldi: orange trees along both kerbs (2019).
GASTAUD_NAME = "Square Théodore Gastaud"
GASTAUD_BANDSTAND = (69.3, 149.6)          # m from ARMES_C
GASTAUD_PAVING = "#c99178"
CONDA_PED_W = 7.0                          # m either side of a pedestrian street's line (walls clip it)
CONDA_SETT = "#6e5a52"
ORANGE_STREETS = ("Rue Grimaldi", "Rue Princesse Caroline")


def condamine_setup(ways):
    """The park and the pedestrian streets (their outline before the
    buildings are cut out), for the pavements to leave alone."""
    zone = condamine_zone()
    park = None
    for t, p, c in ways:
        if c and t.get("name") == GASTAUD_NAME and len(p) > 3:
            park = Polygon(p).buffer(0)
    ped = [(t, LineString(p)) for t, p, c in ways
           if t.get("highway") == "pedestrian" and not c and t.get("area") != "yes" and len(p) >= 2
           and LineString(p).intersects(zone)]
    pp = unary_union([ln.buffer(CONDA_PED_W, cap_style=2) for _, ln in ped]) if ped else Polygon()
    rp, _ = _road_polys(ways)
    pp = pp.difference(unary_union([q for q in rp if q.intersects(zone)] or [Polygon()]))
    return {"zone": zone, "park": park, "ped": ped, "ped_poly": pp}


def _slab(gm, poly, mntR, gz, style, lift=0.08, area=3, uv2f=None):
    n_ = 0.0
    for q in to_polys(poly):
        q = shapely.set_precision(q.simplify(0.05), 0.01)
        if q.is_empty or q.geom_type != "Polygon" or q.area < 1 or not q.is_valid:
            continue
        tr = triangulate(orient(q, 1.0), area=area)
        if tr is None:
            continue
        v, tri = tr
        z = mntR.sample(v[:, 0], v[:, 1])
        if gz is not None:
            fi, fj = (v[:, 0] - X0) / GRID, (Y1 - v[:, 1]) / GRID
            z = np.fmax(z, ndimage.map_coordinates(gz, [fj, fi], order=1, mode="nearest"))
        if not np.isfinite(z).any():
            continue
        z = np.where(np.isfinite(z), z, np.nanmedian(z))
        zt = z[tri]
        tri = tri[(zt.max(1) - zt.min(1)) < 2.0]
        P = enu_fast(v[:, 0], v[:, 1], z + lift)
        uv = np.column_stack([(v[:, 0] - X0) / (X1 - X0), (Y1 - v[:, 1]) / (Y1 - Y0)])
        gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=tri, style=style,
               uv2=None if uv2f is None else uv2f(v[:, 0], v[:, 1]))
        n_ += q.area
    return n_


def bandstand(m, c, z, stats):
    """An octagonal bandstand: a stone base with steps, eight slim white
    cast-iron columns, a railing, a frieze and a white zinc roof rising to
    a finial."""
    R, base_h, col_h = 4.2, 1.0, 2.9
    stone = np.array(hexrgb("#e6dccb"))
    white = np.array(hexrgb("#f2f0ea"))
    ang = np.linspace(0, 2 * math.pi, 9)[:-1] + math.pi / 8
    ring = lambda r_: Polygon([(c[0] + r_ * math.cos(a), c[1] + r_ * math.sin(a)) for a in ang])
    add_walls(m, orient(ring(R), 1.0), z - 0.3, z + base_h, z, 10, stone, PLAIN)
    add_roof(m, orient(ring(R), 1.0), z + base_h, stone * 1.03, False)
    zt = z + base_h
    for a in ang:
        q = (c[0] + (R - 0.3) * math.cos(a), c[1] + (R - 0.3) * math.sin(a))
        _box(m, q[0], q[1], (math.cos(a), math.sin(a)), 0.16, 0.16, zt, zt + col_h, white)
    pr = np.asarray(ring(R - 0.3).exterior.coords)
    A, B = pr[:-1], pr[1:]
    sel = np.arange(len(A)) != 0                 # the way in, facing east
    railing(m, A[sel], B[sel], np.full(sel.sum(), zt), "#f2f0ea", h=0.9)
    fr = orient(ring(R + 0.15), 1.0)
    add_walls(m, fr, zt + col_h - 0.5, zt + col_h, zt, 10, white, PLAIN)
    add_roof(m, orient(ring(R + 0.15).difference(ring(R - 0.2)), 1.0), zt + col_h, white * 0.95, False)
    # Roof: an octagonal bell, flaring at the eaves, to a lantern and finial.
    prof = [(R + 0.6, zt + col_h - 0.15), (R * 0.7, zt + col_h + 0.55), (R * 0.35, zt + col_h + 1.2),
            (0.5, zt + col_h + 1.55)]
    for (r0, z0), (r1, z1) in zip(prof[:-1], prof[1:]):
        p0 = np.array([(c[0] + r0 * math.cos(a), c[1] + r0 * math.sin(a)) for a in ang])
        p1 = np.array([(c[0] + r1 * math.cos(a), c[1] + r1 * math.sin(a)) for a in ang])
        j = np.arange(8); jn = (j + 1) % 8
        E0 = enu_fast(p0[:, 0], p0[:, 1], np.full(8, z0))
        E1 = enu_fast(p1[:, 0], p1[:, 1], np.full(8, z1))
        for sg in (1.0, -1.0):
            _quad(m, E0[j], E0[jn], E1[jn], E1[j], np.array(hexrgb("#e9eaea")) * (1.0 if sg > 0 else 0.7),
                  np.tile([0.0, 0.0, sg], (8, 1)))
    _box(m, c[0], c[1], (1.0, 0.0), 0.7, 0.7, zt + col_h + 1.55, zt + col_h + 2.1, white)
    _ball(m, c[0], c[1], zt + col_h + 2.3, 0.18, np.array(hexrgb("#c9a548")), 6)
    # Steps up on the east side.
    for k in range(4):
        q = (c[0] + R + 0.25 + 0.3 * (3 - k), c[1])
        _box(m, q[0], q[1], (1.0, 0.0), 0.32, 2.2, z - 0.3, z + base_h * (k + 1) / 4, stone)
    stats["bandstands"] = stats.get("bandstands", 0) + 1


def condamine(gm, m, tm, ways, pts, mns, mnt, ortho, gz, fps, stats):
    if CONDA is None:
        return 0
    mntR = Raster(mnt)
    blds = unary_union([p for p in fps if p.intersects(CONDA["zone"].buffer(20))])
    rp, _ = _road_polys(ways)
    roads = unary_union([q for q in rp if q.intersects(CONDA["zone"])])
    rnd = np.random.default_rng(57)
    # Pedestrian streets: limestone slabs, a sett strip down the middle.
    ped = CONDA["ped_poly"].difference(blds.buffer(0.05)).difference(roads)
    if ARMES is not None:
        ped = ped.difference(ARMES["poly"].buffer(0.3))
    if CONDA["park"] is not None:
        ped = ped.difference(CONDA["park"])
    stats["conda_ped_m2"] = round(_slab(gm, ped, mntR, gz, SLAB))
    sett = unary_union([ln.buffer(0.9, cap_style=2) for _, ln in CONDA["ped"]]).intersection(ped.buffer(-1.5))
    stats["conda_sett_m2"] = round(_slab(gm, sett, mntR, gz, None, lift=0.1))
    # Square Gastaud.
    n_ = 0
    if CONDA["park"] is not None:
        park = CONDA["park"]
        stats["gastaud_m2"] = round(_slab(gm, park.difference(blds), mntR, gz, None))
        c = np.array(ARMES_C) + GASTAUD_BANDSTAND
        zc = float(np.nan_to_num(mntR.sample(np.array([c[0]]), np.array([c[1]]))[0], nan=6.9))
        bandstand(m, c, zc, stats)
        # Olive trees where the LiDAR has the square's trees, round the
        # bandstand (not on it).
        stats["gastaud_olives"] = _zone_trees(tm, park.buffer(1.0).difference(Point(*c).buffer(6.0)), mns, mnt, ortho,
                                              fps, lambda h: "olive", hmax=7.0, rad_k=0.8)
        for (x, y), t in pts:
            if t.get("name") == "Le lion et la Sirène" and park.buffer(5).contains(Point(x, y)):
                z = float(np.nan_to_num(mntR.sample(np.array([x]), np.array([y]))[0], nan=zc))
                _box(m, x, y, (1.0, 0.0), 1.6, 1.0, z - 0.2, z + 1.1, np.array(hexrgb("#d9d0bf")))
                _box(m, x, y, (1.0, 0.0), 1.3, 0.6, z + 1.1, z + 2.0, np.array(hexrgb("#5a4a36")), style=6)
                _ball(m, x + 0.35, y, z + 2.2, 0.35, np.array(hexrgb("#5a4a36")), 6)
                stats["gastaud_statue"] = 1
        # Benches round the paths, from the map.
        for (x, y), t in pts:
            if t.get("amenity") == "bench" and park.buffer(2).contains(Point(x, y)):
                z = float(np.nan_to_num(mntR.sample(np.array([x]), np.array([y]))[0], nan=zc))
                _box(m, x, y, (1.0, 0.0), 1.8, 0.5, z + 0.4, z + 0.47, np.array(hexrgb("#6b4a32")))
                _box(m, x, y, (1.0, 0.0), 1.7, 0.4, z - 0.1, z + 0.4, np.array(hexrgb("#2c2a27")))
    # Bitter-orange street trees: in the pedestrian streets (one row, 3 m
    # off the middle, either side in turn) and along Rue Grimaldi's kerbs.
    avoid = blds.buffer(2.2).union(roads.buffer(0.6) if not roads.is_empty else Polygon())
    if ARMES is not None:
        avoid = avoid.union(ARMES["zone"])
    if CONDA["park"] is not None:
        avoid = avoid.union(CONDA["park"].buffer(2))
    placed = []
    for (x, y), t in pts:
        if t.get("natural") == "tree" and CONDA["zone"].contains(Point(x, y)):
            placed.append((x, y))
    lines = [(ln, 3.0, True) for t, ln in CONDA["ped"] if t.get("name") in ORANGE_STREETS]
    for t, p, c in ways:
        if t.get("name") == "Rue Grimaldi" and t.get("highway") in ROAD_W and not t.get("tunnel") and len(p) >= 2:
            ln = LineString(p).intersection(CONDA["zone"])
            for l_ in getattr(ln, "geoms", [ln]):
                if l_.length > 10:
                    lines.append((l_, ROAD_W[t["highway"]] / 2 + 0.9, False))
                    lines.append((l_, -(ROAD_W[t["highway"]] / 2 + 0.9), False))
    n_o = 0
    for ln, off, alt in lines:
        for k, s_ in enumerate(np.arange(4.0, ln.length - 3.0, 9.0)):
            a = np.array(ln.interpolate(s_).coords[0])
            b = np.array(ln.interpolate(min(ln.length, s_ + 0.5)).coords[0])
            d = b - a
            if np.hypot(*d) < 1e-6:
                continue
            nrm = np.array([-d[1], d[0]]) / np.hypot(*d)
            q = a + nrm * (off if not alt or k % 2 == 0 else -off)
            if avoid.contains(Point(*q)) or (placed and min(np.hypot(px_ - q[0], py_ - q[1]) for px_, py_ in placed) < 5):
                continue
            if not off_track(q[0], q[1], 2.0):
                continue
            z = float(mntR.sample(np.array([q[0]]), np.array([q[1]]))[0])
            if not np.isfinite(z):
                continue
            add_tree(tm, q[0], q[1], z, rnd.uniform(4.0, 5.2), rnd.uniform(1.3, 1.7), "orange", rnd)
            placed.append(tuple(q))
            n_o += 1
    stats["conda_orange_trees"] = n_o
    return n_o


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
        if rocher_paved(t, p_):
            continue                    # Le Rocher's paved squares: rocher_streets() lays setts
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



# --------------------------------------------------------------- Fairmont ----
# The Fairmont Monte-Carlo (1975, the old Loews): a long, low hotel on
# land won from the sea, with the Grand Prix tunnel running through its
# foot and its roof a garden at the level of the Casino's lower gardens.
# What the open data gives (IGN LiDAR 2021, aerial photo 2023, OSM lawns
# on the roof, Mapillary 2018/2025 drives through the tunnel and round the
# hairpin):
# - the roof at about 33 m NGF: lawns, pink paving, palms, and two sunken
#   hexagonal courts 11 m down whose floors carry a white star pattern;
#   BD TOPO keeps the north court as a hole in the outline but leaves the
#   south one open to the west, where the garden runs straight on to it;
# - cream walls with a continuous balcony on every floor;
# - on the sea side the tunnel opens behind round columns on to a covered
#   quay at road level, with a sea wall and concrete blocks below.
FAIRMONT = "BATIMENT0000000074074334"
FAIRMONT_PARTS = {FAIRMONT, "BATIMENT0000000074070366", "BATIMENT0000000074070395"}
FAIR_COL = "#ece5d6"                      # cream walls
FAIR_RAIL = "#f6f3ec"                     # white balcony fronts
FAIR_PAVING = "#d6b9a2"                   # the roof garden's pink paving
FAIR_LAWN = "#88aa58"
FAIR = None                               # set by buildings(): the hotel's levels, base, storey height
FM = None                                 # set by fairmont_setup(): courts, rim, garden
OPEN_PARTS = []                           # building parts over the tunnel's open sea side
GROUND_FM = None


def fairmont_setup(feats, mns, mnt):
    """The two sunken courts on the roof and the roof garden's west rim,
    from the LiDAR. Returns a dict, or None."""
    fair = None
    for f in feats:
        if f["properties"].get("cleabs") == FAIRMONT:
            fair = shapely.force_2d(shape(f["geometry"])).buffer(0)
    if fair is None or fair.is_empty:
        return None
    fair = max(to_polys(fair), key=lambda q: q.area)
    courts = [Polygon(h.coords) for h in fair.interiors if Polygon(h.coords).area > 300]
    # The south court: low surface (opened, so palms do not count) next to
    # the outline, outside it, within the outline's reach.
    x0, y0, x1, y1 = fair.buffer(6).bounds
    c0, c1 = max(int((x0 - X0) / RES), 0), int((x1 - X0) / RES)
    r0, r1 = max(int((Y1 - y1) / RES), 0), int((Y1 - y0) / RES)
    S = ndimage.grey_opening(np.nan_to_num(mns[r0:r1, c0:c1], nan=0.0), size=7)
    xs = X0 + (c0 + np.arange(S.shape[1]) + 0.5) * RES
    ys = Y1 - (r0 + np.arange(S.shape[0]) + 0.5) * RES
    XX, YY = np.meshgrid(xs, ys)
    roof_z = float(np.median(S[contains_xy(fair.buffer(-1.5), XX, YY)]))
    low = (S < roof_z - 6) & (S > 5) & ~contains_xy(fair.buffer(0.5), XX, YY) \
        & contains_xy(fair.convex_hull, XX, YY)
    for c in courts:
        low &= ~contains_xy(c.buffer(2), XX, YY)
    low = ndimage.binary_opening(low, iterations=2)
    lab, n = ndimage.label(low)
    open_court = None
    if n:
        sizes = ndimage.sum(low, lab, np.arange(1, n + 1))
        k = int(np.argmax(sizes)) + 1
        if sizes[k - 1] * RES * RES > 600:
            g = cells_to_poly(lab == k, X0 + c0 * RES, Y1 - r0 * RES, RES)
            g = max(to_polys(g), key=lambda q: q.area).convex_hull.difference(fair.buffer(0.3))
            g = max(to_polys(g), key=lambda q: q.area).simplify(0.6)
            open_court = orient(g, 1.0)
            courts.append(open_court)
    floors = []
    for c in courts:
        inner = contains_xy(c.buffer(-2.0), XX, YY)
        floors.append(float(np.median(S[inner])) if inner.sum() > 20 else roof_z - 11)
    out = {"fair": fair, "courts": courts, "floors": floors, "roof_z": roof_z, "open": open_court}
    if open_court is not None:
        # Its west side, where the garden meets it: the court's own wall
        # (BD TOPO stops short of it) and a strip of garden at roof level.
        free = open_court.exterior.difference(fair.buffer(1.5))
        free = [l for l in (free.geoms if hasattr(free, "geoms") else [free]) if l.length > 3]
        if free:
            fl = unary_union(free)
            out["free"] = fl
            out["wall"] = fl.buffer(0.9, cap_style=2).difference(open_court)
            out["rim"] = fl.buffer(5.0, cap_style=2).difference(open_court).difference(fair)
            # Ground nodes near the free edge go: the 3 m grid would
            # slope from the garden into the court.
            out["cut"] = fl.buffer(3.4, cap_style=2).union(open_court)
            zr = S[contains_xy(out["rim"], XX, YY)]
            out["rim_z"] = float(np.median(zr)) if len(zr) else roof_z
    out["garden"] = fair.union(out.get("rim", Polygon())).buffer(0)
    for c in courts:
        out["garden"] = out["garden"].difference(c)
    print(f"Fairmont roof {roof_z:.1f} m, courts", [round(c.area) for c in courts],
          "floors", [round(f, 1) for f in floors], "rim" if "rim" in out else "")
    return out


def _flat_slab(gm, poly, z, style=None):
    """A flat piece of ground (GROUND kind, the painted map)."""
    n = 0
    for pg in to_polys(poly):
        if pg.area < 1:
            continue
        pg = set_precision(pg.simplify(0.03), 0.01) if hasattr(shapely, "set_precision") else pg
        for q in to_polys(pg):
            if q.area < 0.5:
                continue
            tr = triangulate(orient(q, 1.0), area=8)
            if tr is None:
                continue
            v, tri = tr
            zz = z(v[:, 0], v[:, 1]) if callable(z) else np.full(len(v), float(z))
            P = enu_fast(v[:, 0], v[:, 1], zz)
            uv = np.column_stack([(v[:, 0] - X0) / (X1 - X0), (Y1 - v[:, 1]) / (Y1 - Y0)])
            gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=tri, style=style)
            n += 1
    return n


def set_precision(g, grid):
    return shapely.set_precision(g, grid)


def _column(m, x, y, z0, z1, r, col, sides=10):
    """A round column (a many-sided prism)."""
    a = np.linspace(0, 2 * math.pi, sides + 1)
    ring = [(x + r * math.cos(t), y + r * math.sin(t)) for t in a[:-1]]
    add_walls(m, orient(Polygon(ring), 1.0), z0, z1, z0, 100, col, PLAIN)


def fairmont(gm, bm, tm, mns, mnt, sea, stats):
    if FM is None or FAIR is None:
        return 0
    global GROUND_FM
    GROUND_FM = Raster(mnt)
    rnd = rng_for("fairmont")
    col = np.array(hexrgb(FAIR_COL))
    fh, base = FAIR["fh"], FAIR["base"]
    # Court floors, at their own level (the aerial photo is painted there).
    for c, zf in zip(FM["courts"], FM["floors"]):
        _flat_slab(gm, c, zf + 0.05)
    # The south court's west wall, from its floor to the garden, with the
    # hotel's windows, and a white parapet along the top.
    if "wall" in FM:
        zr = FM["rim_z"]
        zf = FM["floors"][-1]
        for q in to_polys(FM["wall"]):
            if q.area > 1:
                add_walls(bm, orient(q, 1.0), zf - 0.5, zr, base, fh, col, FAIR["kind"], style=FAIR["style"])
                add_roof(bm, orient(q, 1.0), zr, col * 0.97, False)
        _flat_slab(gm, FM["rim"], zr + 0.02)
        par = FM["free"].buffer(0.12, cap_style=2).difference(FM["fair"])
        for q in to_polys(par):
            if q.area > 0.2:
                add_walls(bm, orient(q, 1.0), zr, zr + 1.0, zr, 100, np.array(hexrgb(FAIR_RAIL)), PLAIN)
                add_roof(bm, orient(q, 1.0), zr + 1.0, np.array(hexrgb(FAIR_RAIL)), False)
    # The roof garden: on each level's roof, what no higher level covers.
    lv = sorted(FAIR["levels"], key=lambda t: t[1])
    for i, (reg, z) in enumerate(lv):
        top = unary_union([r for r, _ in lv[i + 1:]]) if i + 1 < len(lv) else Polygon()
        g = reg.difference(top).buffer(0)
        _flat_slab(gm, g, z + 0.05)
    # Palms and shrubs on the roof, where the LiDAR has them.
    roofs = unary_union([r for r, _ in lv]).buffer(-1.0)
    zroof = max(z for _, z in lv)
    x0, y0, x1, y1 = roofs.bounds
    c0, c1 = int((x0 - X0) / RES), int((x1 - X0) / RES)
    r0, r1 = int((Y1 - y1) / RES), int((Y1 - y0) / RES)
    S = np.nan_to_num(mns[r0:r1, c0:c1], nan=0.0)
    xs = X0 + (c0 + np.arange(S.shape[1]) + 0.5) * RES
    ys = Y1 - (r0 + np.arange(S.shape[0]) + 0.5) * RES
    XX, YY = np.meshgrid(xs, ys)
    on = contains_xy(roofs, XX, YY)
    for c in FM["courts"]:
        on &= ~contains_xy(c.buffer(1.5), XX, YY)
    hgt = np.where(on, S - zroof, 0)
    crown = ndimage.binary_opening(hgt > 1.8, iterations=1)
    lab, n = ndimage.label(crown)
    nt = 0
    for k in range(1, n + 1):
        mk = lab == k
        a = mk.sum() * RES * RES
        if a < 2.0:
            continue
        h = float(np.percentile(hgt[mk], 90))
        if h > 14:
            continue                    # roof kit, not a tree
        cx, cy = float(XX[mk].mean()), float(YY[mk].mean())
        kind = "palm" if (a < 22 and h > 4) else "pine"
        add_tree(tm, cx, cy, zroof, max(2.5, h), min(4.5, max(1.2, math.sqrt(a / math.pi))), kind, rnd)
        nt += 1
    stats["fairmont_roof_trees"] = nt
    # A balcony with a white front on every floor, all round the outside.
    nb = 0
    hotel = FM["fair"]
    keep = TRACK["line"].buffer(TRACK_HALF + 1.2) if TRACK is not None else None
    keep_z = lambda p_: float(TRACK["z"][TRACK["kd"].query([p_[0], p_[1]])[1]]) if TRACK is not None else -1e9
    for reg, zt in lv:
        for pg in to_polys(reg):
            ring = np.asarray(orient(pg, 1.0).exterior.coords)[:, :2]
            for a_, b_ in zip(ring[:-1], ring[1:]):
                L = float(np.linalg.norm(b_ - a_))
                if L < 5:
                    continue
                u = (b_ - a_) / L
                out = np.array([u[1], -u[0]])
                mid = (a_ + b_) / 2
                if hotel.buffer(-0.2).contains(Point(mid + out * 1.5)):
                    continue                # an inside edge (a court, a step)
                if any(c.buffer(0.5).contains(Point(mid + out * 1.5)) for c in FM["courts"]):
                    continue
                if "rim" in FM and FM["rim"].buffer(0.5).contains(Point(mid + out * 1.5)):
                    continue
                dd_ = np.array([0.8, 2.0, 3.5, 5.0])
                gz_ = GROUND_FM.sample(mid[0] + out[0] * dd_, mid[1] + out[1] * dd_)
                gz_ = float(np.nanmax(gz_)) if np.isfinite(gz_).any() else -1e9
                lift = TUNNEL is not None and ("open_band" in TUNNEL and TUNNEL["open_band"].buffer(1).contains(Point(mid))
                                               or TUNNEL["bld"].contains(Point(mid)))
                zlow = (TUNNEL["top"] if lift else base) + 2.0
                a2, b2 = a_ + u * 0.4, b_ - u * 0.4
                dep = 1.7
                k = 1
                while base + k * fh < zt - 1.2:
                    z = base + k * fh
                    k += 1
                    if z < zlow or (not lift and z < gz_ + 2.0):
                        continue
                    q = Polygon([a2, b2, b2 + out * dep, a2 + out * dep])
                    fr = Polygon([a2 + out * (dep - 0.12), b2 + out * (dep - 0.12), b2 + out * dep, a2 + out * dep])
                    if keep is not None and z < keep_z(mid) + 6.0:
                        q, fr = q.difference(keep), fr.difference(keep)
                    for qq in to_polys(q):
                        if qq.area > 0.5:
                            qq = orient(qq, 1.0)
                            add_walls(bm, qq, z - 0.22, z, z - 0.22, 100, col * 1.02, PLAIN)
                            add_roof(bm, qq, z, col, False)
                            add_roof(bm, orient(qq, -1.0), z - 0.22, col * 0.8, False)
                    for qq in to_polys(fr):
                        if qq.area > 0.05:
                            qq = orient(qq, 1.0)
                            add_walls(bm, qq, z, z + 1.05, z, 100, np.array(hexrgb(FAIR_RAIL)), PLAIN)
                            add_roof(bm, qq, z + 1.05, np.array(hexrgb(FAIR_RAIL)), False)
                    nb += 1
    stats["fairmont_balconies"] = nb
    # The sea side over the tunnel: a covered quay at road level out to the
    # hotel's front, round columns along it, a sea wall below.
    ncol = 0
    stone = np.array(hexrgb("#e4dccb"))
    seawall = np.array(hexrgb("#a8a397"))
    if TUNNEL is not None and OPEN_PARTS:
        from scipy.spatial import cKDTree
        kd = cKDTree(np.column_stack([TUNNEL["x"], TUNNEL["y"]]))
        zroad = lambda x, y: TUNNEL["z"][kd.query(np.column_stack([np.atleast_1d(x), np.atleast_1d(y)]))[1]]
        tline = LineString(np.column_stack([TUNNEL["x"], TUNNEL["y"]]))
        top = TUNNEL["top"]
        for q in to_polys(unary_union([q_ for q_, _ in OPEN_PARTS]).buffer(0.05, join_style=2)):
            deck = q.difference(tline.buffer(TUBE_HALF + 0.8)).buffer(0)
            for d in to_polys(deck):
                if d.area < 4:
                    continue
                _flat_slab(gm, d, lambda x, y: zroad(x, y) + 0.12, style=10)
                # Outer edges: those away from the tunnel.
                ring = np.asarray(orient(d, 1.0).exterior.coords)[:, :2]
                for a_, b_ in zip(ring[:-1], ring[1:]):
                    L = float(np.linalg.norm(b_ - a_))
                    mid = (a_ + b_) / 2
                    if L < 1.0 or tline.distance(Point(mid)) < TUBE_HALF + 2.5:
                        continue
                    u = (b_ - a_) / L
                    out = np.array([u[1], -u[0]])
                    if q.buffer(-0.1).contains(Point(mid + out * 0.8)):
                        continue                    # against the rest of the building
                    zr = float(zroad(*mid)[0]) + 0.12
                    w = orient(Polygon([a_, b_, b_ - out * 0.5, a_ - out * 0.5]), 1.0)
                    add_walls(bm, w, -1.5, zr + 1.0, -1.5, 100, seawall, PLAIN)     # sea wall and parapet
                    add_roof(bm, w, zr + 1.0, stone, False)
                    nn = max(1, int(round(L / 6.5)))
                    for j in range(nn):
                        p_ = a_ + u * (j + 0.5) * L / nn - out * 1.4
                        _column(bm, p_[0], p_[1], zr, top, 0.65, col * 0.98)
                        ncol += 1
        # Concrete blocks along the shore at the foot of the sea wall.
        band = unary_union([q for q, _ in OPEN_PARTS]).buffer(9.0)
        seaB = ndimage.binary_dilation(sea, iterations=2) & ~sea
        rr, cc = np.nonzero(seaB)
        bx, by = X0 + (cc + 0.5) * RES, Y1 - (rr + 0.5) * RES
        ins = contains_xy(band, bx, by)
        nbk = 0
        grey = np.array(hexrgb("#9d9a92"))
        for x, y in zip(bx[ins][::3], by[ins][::3]):
            for lay in range(3):
                xx, yy = x + rnd.uniform(-2.0, 2.0), y + rnd.uniform(-2.0, 2.0)
                s_ = rnd.uniform(0.9, 1.6)
                th = rnd.uniform(0, math.pi)
                bq = shapely.affinity.rotate(box(xx - s_, yy - s_, xx + s_, yy + s_), th, use_radians=True)
                z0 = rnd.uniform(-1.0, 0.4) + 1.3 * lay
                add_walls(bm, orient(bq, 1.0), z0 - 1.5, z0 + s_ * 1.4, z0, 100, grey * rnd.uniform(0.85, 1.05), PLAIN)
                add_roof(bm, orient(bq, 1.0), z0 + s_ * 1.4, grey * 1.04, False)
                nbk += 1
        stats["fairmont_blocks"] = nbk
    stats["fairmont_columns"] = ncol
    return nb


def _paint_garden(arr, px, ways, hi_, garden, base_hex, lawn_hex, paths=False, shade_lawn=False):
    """A garden painted crisp into the ground map: a base (paving or
    gravel), lawns where OpenStreetMap maps them or the 20 cm photo is
    green (tidied into clean beds), a pale kerb round each, and with
    `paths` the mapped footways and drives drawn back on top."""
    S = arr.shape[0]
    xy = lambda pts: [((x - X0) * px, (Y1 - y) * px) for x, y in pts]
    mk = Image.new("L", (S, S), 0)
    lawn = Image.new("L", (S, S), 0)
    dm, dl = ImageDraw.Draw(mk), ImageDraw.Draw(lawn)
    for q in to_polys(garden):
        dm.polygon(xy(q.exterior.coords), fill=255)
        for h in q.interiors:
            dm.polygon(xy(h.coords), fill=0)
    for t, pts, closed in ways:
        if closed and len(pts) > 3 and t.get("landuse") in ("grass", "flowerbed"):
            g = Polygon(pts).buffer(0)
            if g.intersects(garden):
                for q in to_polys(g.intersection(garden.buffer(-0.6))):
                    dl.polygon(xy(q.exterior.coords), fill=255)
    x0_, y0_, x1_, y1_ = garden.bounds
    a0, b0 = max(int((x0_ - X0) * px) - 2, 0), max(int((Y1 - y1_) * px) - 2, 0)
    a1, b1 = min(int((x1_ - X0) * px) + 3, S), min(int((Y1 - y0_) * px) + 3, S)
    if hi_ is not None:
        # (Lawns are often a dull grey-green in the photo, under trees too.)
        o_ = hi_[b0:b1, a0:a1]
        veg = (2 * o_[..., 1] - o_[..., 0] - o_[..., 2] > 7) & (o_.mean(-1) < 160) & (o_[..., 1] > o_[..., 0])
        if shade_lawn:
            # In a park the dark of the photo is the trees' shade: plants.
            veg |= o_.mean(-1) < 85
        veg = ndimage.binary_closing(ndimage.binary_opening(veg, iterations=2), iterations=3)
        veg = ndimage.binary_opening(veg, iterations=3)
        lw_ = np.asarray(lawn).copy()
        lw_[b0:b1, a0:a1] = np.maximum(lw_[b0:b1, a0:a1], (veg * 255).astype(np.uint8))
        lawn = Image.fromarray(lw_)
    if paths:
        dl = ImageDraw.Draw(lawn)
        for t, pts, closed in ways:
            hw = t.get("highway")
            if hw and not closed and t.get("tunnel") is None and t.get("layer", "0") not in ("-1", "-2") \
                    and LineString(pts).intersects(garden):
                w = max(1, int((2.2 if hw in ("footway", "path", "steps", "pedestrian") else 5.0) * px))
                dl.line(xy(pts), fill=0, width=w, joint="curve")
    lawn = ImageChops.multiply(lawn, mk.filter(ImageFilter.MinFilter(5)))
    sl = (slice(b0, b1), slice(a0, a1))
    m = np.asarray(mk).astype(np.float32)[sl][..., None] / 255
    lw = np.asarray(lawn.filter(ImageFilter.GaussianBlur(1))).astype(np.float32)[sl][..., None] / 255
    rng = np.random.default_rng(11)
    h_, w_ = m.shape[:2]
    noise = np.asarray(Image.fromarray(rng.normal(0, 1, (max(2, h_ // 8), max(2, w_ // 8))).astype(np.float32)).resize((w_, h_), Image.BILINEAR))[..., None]
    pav = np.array(hexrgb(base_hex), np.float32) * (1 + 0.03 * noise)
    grn = np.array(hexrgb(lawn_hex), np.float32) * (1 + 0.06 * noise)
    g = pav * (1 - lw) + grn * lw
    # A pale stone kerb round each lawn.
    edge = np.asarray(lawn.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.MaxFilter(3))).astype(np.float32)[sl][..., None] / 255
    g = g * (1 - edge * 0.8) + np.array(hexrgb("#ece4d4"), np.float32) * edge * 0.8
    # Keep a little of the map's shade (tree and building shadows), softly.
    old = arr[sl]
    lum = old.mean(-1, keepdims=True) / max(float(np.median(old.mean(-1))), 1.0)
    g = g * np.clip(0.75 + 0.25 * lum, 0.7, 1.05)
    arr[sl] = old * (1 - m) + g * m
    return arr


CASINO_GARDENS = ("Jardin d‘acclimatation", "Jardin d'acclimatation")


def fairmont_paint(arr, px, ways, ortho_hi):
    """The roof garden in the ground map: pink paving, lawns, paths; the
    Casino's lower gardens next to it crisp too; the courts' floors from
    the aerial photo."""
    if FM is None:
        return arr
    S = arr.shape[0]
    xy = lambda pts: [((x - X0) * px, (Y1 - y) * px) for x, y in pts]
    hi_ = None
    if ortho_hi is not None:
        hi_ = np.asarray(ortho_hi.resize((S, S), Image.BILINEAR) if ortho_hi.size != (S, S) else ortho_hi).astype(np.float32)
    for t, pts, closed in ways:
        if closed and len(pts) > 3 and t.get("name") in CASINO_GARDENS and not (
                PARKS is not None and t.get("name") in PARKS["names"]):     # (laid by parks() now)
            g = Polygon(pts).buffer(0).difference(FM["fair"].buffer(0.5))
            arr = _paint_garden(arr, px, ways, hi_, g, "#d2c8b2", "#82a456", paths=True, shade_lawn=True)
    arr = _paint_garden(arr, px, ways, hi_, FM["garden"], FAIR_PAVING, FAIR_LAWN)
    # The courts' floors: the 20 cm photo (white star on grey gravel).
    if ortho_hi is not None:
        hi = ortho_hi.resize((S, S), Image.LANCZOS) if ortho_hi.size != (S, S) else ortho_hi
        for c in FM["courts"]:
            bx0, by0, bx1, by1 = c.bounds
            a0, b0 = int((bx0 - X0) * px), int((Y1 - by1) * px)
            a1, b1 = int((bx1 - X0) * px) + 1, int((Y1 - by0) * px) + 1
            patch = np.asarray(hi.crop((a0, b0, a1, b1))).astype(np.float32)
            pm = Image.new("L", (a1 - a0, b1 - b0), 0)
            ImageDraw.Draw(pm).polygon([(X - a0, Y - b0) for X, Y in xy(c.exterior.coords)], fill=255)
            pmf = np.asarray(pm).astype(np.float32)[..., None] / 255
            # Lift the photo's shadows a little (the court is lit in the set).
            patch = np.clip((patch - 110) * 1.05 + 140, 0, 255)
            sub = arr[b0:b1, a0:a1]
            arr[b0:b1, a0:a1] = sub * (1 - pmf) + patch * pmf
    return arr


# ------------------------------------------------------------------- main ----# ------------------------------------------------------------ Fontvieille ----
# Stade Louis II (1985): a pitch raised 13 m on a concrete platform over car
# parks, a sports hall and pools; office blocks all round it under big
# terracotta pyramid roofs (cream frames, ochre spandrels, ribbon windows);
# the uncovered away end with its yellow seats and the nine white arches.
# One BD TOPO outline for the whole, plus the away end as a "Tribune".
# Frame (from the LiDAR): s along the pitch's long axis toward the north-east
# end, t across it toward the south-east stand.
STADE = "BATIMENT0000000074079083"
STADE_POLY = None
STADE_DONE = []
STADE_TRIB = "BATIMENT0000000074079011"
STADE_O = (1055655.0, 6302001.5)
STADE_U = (112.0, 126.0)
STADE_PITCH_Z = 13.25                  # m NGF, the platform (LiDAR)
STADE_NW, STADE_SE, STADE_NE = -38.0, 42.0, 82.0   # the roofed stands' front edges (t, t, s)
STADE_SW = -70.0                       # where the roofed corners of the away end begin (s)
STADE_C = (-29.5, 0.0)                 # centre of the away end's curves
STADE_AWAY = (46.5, 67.0)              # radii: its front row, its back
STADE_AWAY_T = 28.5                    # half its width, m
STADE_ARCH_R = 64.0                    # radius of the line of arches
STADE_ARCH_TOP = 44.0                  # crowns (LiDAR)
STADE_ARCHES = 9
STADE_ARCH_SPAN = 6.4                  # centre to centre (LiDAR crowns)
STADE_PITCH = (-43.0, 62.0, -35.0, 33.0)   # 105 x 68 m (s0, s1, t0, t1), from the 20 cm photo's grass
STADE_WALL = "#d7ad74"                 # ochre spandrels (Mapillary 2019, 2021)
STADE_FRAME = "#eadfc9"                # cream concrete frame
STADE_SEAT = "#b3262c"
STADE_AWAY_SEAT = "#e2b83a"


def stade_setup(feats):
    g = [shapely.force_2d(shape(f["geometry"])).buffer(0.05) for f in feats
         if f["properties"].get("cleabs") in (STADE, STADE_TRIB)]
    if not g:
        return None
    out = unary_union(g).buffer(-0.05)
    out = max(to_polys(out), key=lambda q: q.area)
    return orient(Polygon(out.exterior), 1.0)


def stade_open(F):
    """The open part, in plan: the bowl and the away end."""
    c_s, c_t = STADE_C
    R = STADE_AWAY[1]
    tt = np.linspace(STADE_AWAY_T, -STADE_AWAY_T, 25)
    arc = [(c_s - math.sqrt(R * R - t * t), t) for t in tt]
    pts = [(STADE_SW, STADE_NW), (STADE_NE, STADE_NW), (STADE_NE, STADE_SE), (STADE_SW, STADE_SE),
           (STADE_SW, STADE_AWAY_T)] + arc + [(STADE_SW, -STADE_AWAY_T)]
    return orient(Polygon([F.xy(a, b) for a, b in pts]), 1.0)


def _strip_wall(m, xs, ys, zb, zt, col, kind=PLAIN, style=None, fh=3.2, bay=3.6, out=None):
    """Walls along a polyline whose foot (zb) and top (zt) vary point by
    point, facing out (a (2,) plan direction test: the side away from out)."""
    xs, ys, zb, zt = (np.asarray(a, float) for a in (xs, ys, zb, zt))
    k = len(xs) - 1
    if k < 1:
        return
    a = np.column_stack([xs[:-1], ys[:-1]])
    b = np.column_stack([xs[1:], ys[1:]])
    L = np.linalg.norm(b - a, axis=1)
    okk = (L > 0.05) & (np.maximum(zt[:-1], zt[1:]) > np.minimum(zb[:-1], zb[1:]) + 0.05)
    s0 = np.r_[0.0, np.cumsum(L)[:-1]]
    P = enu_fast(np.column_stack([a[:, 0], b[:, 0], b[:, 0], a[:, 0]]).ravel(),
                 np.column_stack([a[:, 1], b[:, 1], b[:, 1], a[:, 1]]).ravel(),
                 np.column_stack([zb[:-1], zb[1:], zt[1:], zt[:-1]]).ravel()).reshape(k, 4, 3)[okk]
    d = (b - a)[okk] / L[okk, None]
    nplan = np.column_stack([d[:, 1], -d[:, 0]])            # right of the direction of travel
    if out is not None:
        mid = (a[okk] + b[okk]) / 2
        flip = ((mid - out) * nplan).sum(1) < 0
        nplan[flip] *= -1
    want = enu_fast(((a[okk] + b[okk]) / 2)[:, 0] + nplan[:, 0], ((a[okk] + b[okk]) / 2)[:, 1] + nplan[:, 1],
                    np.zeros(okk.sum())) - enu_fast(((a[okk] + b[okk]) / 2)[:, 0], ((a[okk] + b[okk]) / 2)[:, 1],
                                                    np.zeros(okk.sum()))
    base = zb[:-1][okk]
    u0, u1 = s0[okk] / bay, (s0[okk] + L[okk]) / bay
    uv = [np.column_stack([u0, (zb[:-1][okk] - base) / fh]), np.column_stack([u1, (zb[1:][okk] - base) / fh]),
          np.column_stack([u1, (zt[1:][okk] - base) / fh]), np.column_stack([u0, (zt[:-1][okk] - base) / fh])]
    _quad_uv(m, P[:, 0], P[:, 1], P[:, 2], P[:, 3], np.asarray(col, float), want, uv, kind=kind, style=style)


def stade_louis_ii(m, mns, mntR, photo, outline, stats):
    F = Frame(m, STADE_O, STADE_U)
    zp = STADE_PITCH_Z
    wall = np.array(hexrgb(STADE_WALL), float)
    frame_c = np.array(hexrgb(STADE_FRAME), float)
    seat = np.array(hexrgb(STADE_SEAT), float)
    white = np.array(hexrgb("#eeeae2"), float)
    concrete = np.array(hexrgb("#b9b2a6"), float)
    dark = np.array(hexrgb("#4b4640"), float)
    open_ = stade_open(F)
    roof = outline.difference(open_.buffer(0.01, join_style=2))
    roof_parts = [orient(q, 1.0) for q in to_polys(roof) if q.area > 40]
    ctr = np.array(F.xy(0.0, 0.0))

    # --- the roofs: rows of terracotta pyramids (the 20 cm photo: five or
    # six to a long stand, valleys between), each hipped from one eave up
    # to the LiDAR's top for that piece, painted with the photo; the outer
    # walls built up to meet the eaves.
    S = Raster(ndimage.median_filter(np.nan_to_num(mns, nan=0.0), size=5))
    cuts_s = [-300.0, STADE_SW, -40.0, -10.0, 20.0, 51.0, STADE_NE, 300.0]
    regions = []
    for t0, t1 in ((-300.0, STADE_NW), (STADE_SE, 300.0)):
        regions += [F.poly(a_, b_, t0, t1) for a_, b_ in zip(cuts_s[:-1], cuts_s[1:])]
    regions += [F.poly(STADE_NE, 300.0, STADE_NW, 2.0), F.poly(STADE_NE, 300.0, 2.0, STADE_SE),
                F.poly(-300.0, STADE_SW, STADE_NW, 0.0), F.poly(-300.0, STADE_SW, 0.0, STADE_SE)]
    pieces = []
    for rg in regions:
        for part in roof_parts:
            for q in to_polys(part.intersection(rg)):
                if q.area > 30:
                    pieces.append(orient(q.simplify(0.3), 1.0))
    edges, eaves = [], []
    slope = math.tan(math.radians(17))
    for q in pieces:
        x0, y0, x1, y1 = q.bounds
        gx, gy = np.meshgrid(np.arange(x0, x1, 1.0), np.arange(y0, y1, 1.0))
        ins = contains_xy(q.buffer(-1.0), gx, gy)
        zi = S.sample(gx[ins], gy[ins]) if ins.any() else np.array([np.nan])
        ring = q.buffer(-1.2).exterior if isinstance(q.buffer(-1.2), Polygon) else q.exterior
        rp = shapely.line_interpolate_point(ring, np.linspace(0, 1, 80), normalized=True)
        ze = S.sample(shapely.get_x(rp), shapely.get_y(rp))
        eave = float(np.nanpercentile(ze, 20)) if np.isfinite(ze).any() else 31.0
        top = float(np.nanpercentile(zi, 97)) if np.isfinite(zi).any() else eave + 4
        eave = min(max(eave, zp + 4.0), 40.0)
        top = max(top, eave + 1.0)
        eaves.append(eave)
        zg = mntR.sample(*np.asarray(q.exterior.coords)[:, :2].T)
        base_ = float(np.nanmin(zg)) if np.isfinite(zg).any() else 5.0
        rs = skeleton_roof(q, eave, slope, (top - eave) / slope, dt=0.5)
        if rs is None:
            add_roof(m, q, eave, hexrgb("#c8754a"), False)
        else:
            v, zz, tri = rs
            P = enu_fast(v[:, 0], v[:, 1], zz)
            if photo is not None:
                lx, ly = photo["lean"]
                ph = dict(photo, shift=((eave - base_) * lx, (eave - base_) * ly))
                dz = zz - eave
                uv = np.column_stack([v[:, 0] - X0 + dz * lx, Y1 - v[:, 1] - dz * ly])
                photo["mesh"].tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], [255, 255, 255], ROOF_FLAT,
                                  uv=np.stack([uv[tri[:, 0]], uv[tri[:, 1]], uv[tri[:, 2]]], 1), style=PHOTO_ROOF)
                photo["uvmap"].append((len(photo["mesh"].uv) - 1, len(photo["polys"])))
                photo["polys"].append((q, ph["shift"], eave))
                photo["tiled"].append(True)
            else:
                m.tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], hexrgb("#c8754a"), ROOF_TILE)
        rl = q.exterior
        n_ = max(4, int(rl.length / 1.0))
        rq = shapely.line_interpolate_point(rl, np.linspace(0, rl.length, n_, endpoint=False))
        edges.append((shapely.get_x(rq), shapely.get_y(rq), np.full(n_, eave)))
    if not edges:
        return outline
    outer = outline.exterior
    open_b = open_.exterior
    n_out = 0
    for bx, by, ze in edges:
        bx, by, ze = np.r_[bx, bx[:1]], np.r_[by, by[:1]], np.r_[ze, ze[:1]]
        mid = shapely.points((bx[:-1] + bx[1:]) / 2, (by[:-1] + by[1:]) / 2)
        on_out = shapely.distance(outer, mid) < 0.6
        on_open = shapely.distance(open_b, mid) < 0.6
        # Runs of segments on the outline: office façades from the street.
        for flag, kind_ in ((on_out, "out"), (on_open & ~on_out, "in"), (~on_open & ~on_out, "cut")):
            idx = np.nonzero(flag)[0]
            if not len(idx):
                continue
            runs = np.split(idx, np.nonzero(np.diff(idx) > 1)[0] + 1)
            for r_ in runs:
                j = np.r_[r_, r_[-1] + 1]
                xs, ys, zt = bx[j], by[j], ze[j]
                if kind_ == "cut":
                    # Between two roof pieces: a gable wall up to this one's
                    # eave (hidden where the neighbour is as high).
                    for sg_ in (1, -1):
                        _strip_wall(m, xs[::sg_], ys[::sg_], np.full(len(xs), 5.0), zt[::sg_], frame_c * 0.95)
                    continue
                if kind_ == "out":
                    zb = mntR.sample(xs, ys)
                    zb = np.where(np.isfinite(zb), zb, 5.0) - 0.3
                    # Ribbon-window offices up to a cream cornice; the ground floor arcade is a darker band.
                    _strip_wall(m, xs, ys, zb, np.maximum(zt - 1.2, zb + 3), wall, WALL_NEW, S_RIBBON, 3.25, 3.6, out=ctr)
                    _strip_wall(m, xs, ys, np.maximum(zt - 1.2, zb + 3), zt, frame_c, out=ctr)
                    n_out += len(r_)
                else:
                    # Faces toward the pitch: a cream fascia under the roof
                    # edge over the three roofed stands (their seating is
                    # built by stade_stands), pale concrete elsewhere.
                    s_, t_ = F_st(F, xs, ys)
                    main = bool(np.all((np.abs(t_ - STADE_NW) < 0.8) | (np.abs(t_ - STADE_SE) < 0.8)
                                       | (np.abs(s_ - STADE_NE) < 0.8)))
                    zb = (zt - 2.6) if main else np.full(len(xs), zp)
                    away = 2 * np.array([xs.mean(), ys.mean()]) - ctr      # face the pitch
                    _strip_wall(m, xs, ys, zb, zt, frame_c if main else concrete, out=away)
    stats["stade_wall_m"] = n_out
    stade_floor(F, m, zp)
    stade_stands(F, m, mns, seat, white, dark, frame_c)
    stade_away(F, m, zp, white)
    stats["landmarks"] = stats.get("landmarks", 0) + 1
    stats["stade_louis_ii"] = 1
    return outline


def F_st(F, x, y):
    d = np.column_stack([np.asarray(x) - F.O[0], np.asarray(y) - F.O[1]])
    return d @ F.u, d @ F.v


def stade_floor(F, m, zp):
    """The platform: grass in mown stripes, the white lines, red running
    track round the away curve and along the sides, grey aprons; goals."""
    s0, s1, t0, t1 = STADE_PITCH
    c_s, c_t = STADE_C
    grass = [np.array(hexrgb(c), float) for c in ("#3f7a34", "#4a8a3c")]
    red = np.array(hexrgb("#b0473a"), float)
    grey = np.array(hexrgb("#a9a59c"), float)
    white = np.array(hexrgb("#f1f0ea"), float)
    open_ = stade_open(F)
    st = 1.0
    S_, T_ = np.meshgrid(np.arange(-100.0, 84.0, st), np.arange(STADE_NW - 1, STADE_SE + 1, st))
    S_, T_ = S_.ravel(), T_.ravel()
    sc, tc = S_ + st / 2, T_ + st / 2
    xy = F.O[None] + sc[:, None] * F.u[None] + tc[:, None] * F.v[None]
    ins = contains_xy(open_.buffer(0.6), xy[:, 0], xy[:, 1])
    S_, T_, sc, tc = S_[ins], T_[ins], sc[ins], tc[ins]
    R = np.hypot(sc - c_s, tc - c_t)
    col = np.tile(grey, (len(sc), 1))
    pitch = (sc >= s0 - 1) & (sc < s1 + 1) & (tc >= t0 - 1) & (tc < t1 + 1)
    stripe = (np.floor((sc - s0) / 5.25).astype(int) % 2)
    col[pitch] = np.where(stripe[pitch, None] == 0, grass[0], grass[1])
    # The track: 8 lanes round the away curve, on to the sides.
    track = ((sc < s0 - 1) & (R > 37.0) & (R < 46.5)) | (~pitch & ((tc < t0 - 1) | (tc >= t1 + 1)) & (sc < STADE_NE - 14))
    col[track] = red
    # The long-jump runway on the north-east apron.
    runway = (sc > s1 + 3) & (np.abs(tc - (-14.0)) < 0.7 + 0.6) & (sc < STADE_NE - 1)
    col[runway] = red * 0.95
    k = len(sc)
    zf = zp + 0.06
    P0, P1, P2, P3 = F.E(S_, T_, zf), F.E(S_ + st, T_, zf), F.E(S_ + st, T_ + st, zf), F.E(S_, T_ + st, zf)
    up = F.want(0.0, 0.0, zp, 0, 0, 1)
    _quad(m, P0, P1, P2, P3, col, np.tile(up, (k, 1)))

    # White lines (12 cm), 2 cm above the grass.
    zl = zp + 0.08
    w = 0.12

    def line(a, b):
        a, b = np.asarray(a, float), np.asarray(b, float)
        d = b - a
        L = np.hypot(*d)
        if L < 1e-6:
            return
        n_ = np.array([-d[1], d[0]]) / L * w / 2
        F.quad((*(a - n_), zl), (*(b - n_), zl), (*(b + n_), zl), (*(a + n_), zl), white, (0, 0, 1))

    def rect(a0, a1, b0, b1):
        for p, q in (((a0, b0), (a1, b0)), ((a1, b0), (a1, b1)), ((a1, b1), (a0, b1)), ((a0, b1), (a0, b0))):
            line(p, q)

    def arc(cs, ct, r, th0, th1, n=24):
        th = np.linspace(th0, th1, n + 1)
        for i in range(n):
            line((cs + r * math.cos(th[i]), ct + r * math.sin(th[i])), (cs + r * math.cos(th[i + 1]), ct + r * math.sin(th[i + 1])))

    sm, tm = (s0 + s1) / 2, (t0 + t1) / 2
    rect(s0, s1, t0, t1)
    line((sm, t0), (sm, t1))
    arc(sm, tm, 9.15, 0, 2 * math.pi, 32)
    for end, sg in ((s0, 1), (s1, -1)):
        rect(end, end + sg * 16.5, tm - 20.16, tm + 20.16)
        rect(end, end + sg * 5.5, tm - 9.16, tm + 9.16)
        a0 = math.acos(5.5 / 9.15)           # the penalty arc outside the box
        arc(end + sg * 11.0, tm, 9.15, (-a0 if sg > 0 else math.pi - a0), (a0 if sg > 0 else math.pi + a0), 10)
        # The goal: posts, crossbar and a grey net box behind.
        for dt in (-3.66, 3.66):
            F.box(end - 0.06, end + 0.06, tm + dt - 0.06, tm + dt + 0.06, zp, zp + 2.44, white)
        F.box(end - 0.06, end + 0.06, tm - 3.72, tm + 3.72, zp + 2.38, zp + 2.5, white)
        F.box(end - sg * 2.0, end - sg * 0.08, tm - 3.66, tm + 3.66, zp + 0.05, zp + 2.0,
              np.array(hexrgb("#d9d9d4"), float), top=True, sides="tT" + ("s" if sg > 0 else "S"))
    # Lane lines round the curve.
    for k_ in range(9):
        r_ = 37.0 + k_ * (46.5 - 37.0) / 8
        th_ = math.asin(min(1.0, (t1 + 1) / r_))
        arc(c_s, c_t, r_, math.pi - th_, math.pi + th_, 20)


def stade_stands(F, m, mns, seat, white, dark, frame_c):
    """Under the three roofs: raked rows of red seats (white every few rows),
    a dark back wall and a grey soffit up to the cream fascia."""
    zp = STADE_PITCH_Z
    S = _MNS_S.get(id(mns))
    if S is None:
        return
    soffit_c = np.array(hexrgb("#8f8a82"), float)
    stair = np.array(hexrgb("#9a948a"), float)
    # (front line: along a, at c; out: +1/-1 = which way the stand climbs;
    # axis "t": the row runs along s at t = c; "s": runs along t at s = c).
    sides = [("t", STADE_NW, -1, STADE_SW + 2, STADE_NE - 2, 22.0),
             ("t", STADE_SE, +1, STADE_SW + 2, STADE_NE - 2, 22.0),
             ("s", STADE_NE, +1, STADE_NW + 2, STADE_SE - 2, 16.0)]
    for ax, c, out, a0, a1, depth in sides:
        def P(a, d, z):
            return (a, c + out * d, z) if ax == "t" else (c + out * d, a, z)

        def n_(dd, dz):
            return (0, out * dd, dz) if ax == "t" else (out * dd, 0, dz)
        # Roof underside height along the back of the stand.
        aa = np.linspace(a0, a1, 40)
        pts = [F.xy(*P(a, depth, 0)[:2]) for a in aa] + [F.xy(*P(a, 0.5, 0)[:2]) for a in aa]
        zz = np.array([S[int((Y1 - y) / RES), int((x - X0) / RES)] for x, y in pts])
        z_back = float(np.percentile(zz[:40], 5)) - 1.2
        z_front = float(np.percentile(zz[40:], 5)) - 2.6
        rd, rise = 0.8, 0.42
        nrow = int(depth / rd)
        z0 = zp + 1.4
        d_top = rd
        # Front wall (the barrier) from the pitch to the first row.
        F.quad(P(a0, 0, zp), P(a1, 0, zp), P(a1, 0, z0), P(a0, 0, z0), frame_c, n_(-1, 0))
        z_top = z0
        for k in range(nrow):
            z = z0 + k * rise
            if z + rise > z_back - 2.5:
                break
            d0, d1 = k * rd, (k + 1) * rd
            col = white if k % 9 == 8 else seat * (0.96 + 0.04 * (k % 2))
            F.quad(P(a0, d0, z), P(a1, d0, z), P(a1, d1, z), P(a0, d1, z), col, n_(0, 1))
            F.quad(P(a0, d1, z), P(a1, d1, z), P(a1, d1, z + rise), P(a0, d1, z + rise), col * 0.78, n_(-1, 0))
            z_top = z + rise
            d_top = d1
        # Stairways every 14 m: grey strips over the seats.
        for a in np.arange(a0 + 7.0, a1 - 3.0, 14.0):
            F.quad(P(a - 0.6, 0, z0 + 0.02), P(a + 0.6, 0, z0 + 0.02), P(a + 0.6, d_top, z_top + 0.02),
                   P(a - 0.6, d_top, z_top + 0.02), stair, n_(-0.3, 1))
        # Back wall up to the soffit, the soffit out to the fascia.
        F.quad(P(a0, d_top, z_top), P(a1, d_top, z_top), P(a1, d_top, z_back), P(a0, d_top, z_back), dark, n_(-1, 0))
        F.quad(P(a0, d_top, z_back), P(a1, d_top, z_back), P(a1, 0.0, z_front), P(a0, 0.0, z_front), soffit_c,
               n_(0, -1))
        # The stand's two ends.
        for a, sg in ((a0, -1), (a1, 1)):
            q = [P(a, 0, zp), P(a, d_top, zp), P(a, d_top, z_back), P(a, 0, z_front)]
            nn = (sg, 0, 0) if ax == "t" else (0, sg, 0)
            F.quad(*q, np.array(hexrgb("#cfc6b6"), float), nn)


def stade_away(F, m, zp, white):
    """The uncovered away end: yellow rows on the curve, a white parapet
    at the back, and the nine white arches standing over it."""
    c_s, c_t = STADE_C
    r0, r1 = STADE_AWAY
    yel = np.array(hexrgb(STADE_AWAY_SEAT), float)
    grey = np.array(hexrgb("#b5ada0"), float)
    rd, rise = 0.82, 0.27
    T = STADE_AWAY_T

    def ring(r, z, n=48):
        th = math.asin(min(1.0, T / r))
        a = np.linspace(math.pi - th, math.pi + th, n + 1)
        return [(c_s + r * math.cos(x), c_t + r * math.sin(x), z) for x in a], a

    nrow = int((r1 - 0.8 - r0) / rd)
    z0 = zp + 1.1
    p, a = ring(r0, zp)
    q, _ = ring(r0, z0)
    for i in range(48):
        xm = (a[i] + a[i + 1]) / 2
        F.quad(p[i], p[i + 1], q[i + 1], q[i], grey, (-math.cos(xm), -math.sin(xm), 0))
    zt = z0
    for k in range(nrow):
        ra, rb = r0 + k * rd, r0 + (k + 1) * rd
        z = z0 + k * rise
        A, a = ring(ra, z)
        B, _ = ring(rb, z)
        C, _ = ring(rb, z + rise)
        col = grey if k % 10 == 9 else yel * (0.97 + 0.03 * (k % 2))
        for i in range(48):
            xm = (a[i] + a[i + 1]) / 2
            # Aisles: every 8th sector is grey steps.
            cc = grey if i % 8 == 4 else col
            F.quad(A[i], A[i + 1], B[i + 1], B[i], cc, (0, 0, 1))
            F.quad(B[i], B[i + 1], C[i + 1], C[i], cc * 0.8, (-math.cos(xm), -math.sin(xm), 0))
        zt = z + rise
        r_top = rb
    # Back terrace and parapet.
    A, a = ring(r_top, zt)
    B, _ = ring(r1 + 0.6, zt)
    for i in range(48):
        F.quad(A[i], A[i + 1], B[i + 1], B[i], grey, (0, 0, 1))
    Pp, _ = ring(r1 + 0.6, zt + 1.1)
    for i in range(48):
        xm = (a[i] + a[i + 1]) / 2
        F.quad(B[i], B[i + 1], Pp[i + 1], Pp[i], white, (-math.cos(xm), -math.sin(xm), 0))
    # The arches: inverted U frames side by side on a curve, crowns at 44 m.
    R = STADE_ARCH_R
    z_base = z0 + (R - r0) / rd * rise
    half = STADE_ARCHES * STADE_ARCH_SPAN / 2
    ro = STADE_ARCH_SPAN / 2                 # outer radius: neighbours touch at the piers
    ri = ro - 0.8
    zs = STADE_ARCH_TOP - ro
    dep = 1.0
    for k in range(STADE_ARCHES):
        tl, tr = -half + k * STADE_ARCH_SPAN, -half + (k + 1) * STADE_ARCH_SPAN
        sl = c_s - math.sqrt(R * R - tl * tl)
        sr = c_s - math.sqrt(R * R - tr * tr)
        p0, p1 = np.array([sl, tl]), np.array([sr, tr])
        d = (p1 - p0) / np.linalg.norm(p1 - p0)
        nrm = np.array([-d[1], d[0]])          # toward the pitch (+s side)
        if nrm[0] < 0:
            nrm = -nrm
        mid = (p0 + p1) / 2
        span = np.linalg.norm(p1 - p0)
        k_ = span / STADE_ARCH_SPAN
        roo, rii = ro * k_, ri * k_

        def at(x, z, off):
            pp = mid + d * x + nrm * off
            return (pp[0], pp[1], z)
        # Legs.
        for x0_, x1_ in ((-roo, -rii), (rii, roo)):
            for off, sg in ((dep / 2, 1), (-dep / 2, -1)):
                F.quad(at(x0_, z_base, off), at(x1_, z_base, off), at(x1_, zs, off), at(x0_, zs, off), white,
                       (nrm[0] * sg, nrm[1] * sg, 0))
            for xx, sg in ((x0_, -1), (x1_, 1)):
                F.quad(at(xx, z_base, dep / 2), at(xx, z_base, -dep / 2), at(xx, zs, -dep / 2), at(xx, zs, dep / 2),
                       white * 0.92, (d[0] * sg, d[1] * sg, 0))
        # The round head.
        th = np.linspace(0, math.pi, 17)
        for i in range(16):
            for off, sg in ((dep / 2, 1), (-dep / 2, -1)):
                F.quad(at(rii * math.cos(th[i]), zs + rii * math.sin(th[i]), off),
                       at(rii * math.cos(th[i + 1]), zs + rii * math.sin(th[i + 1]), off),
                       at(roo * math.cos(th[i + 1]), zs + roo * math.sin(th[i + 1]), off),
                       at(roo * math.cos(th[i]), zs + roo * math.sin(th[i]), off), white,
                       (nrm[0] * sg, nrm[1] * sg, 0))
            xm = (th[i] + th[i + 1]) / 2
            for rr, sg, sh in ((roo, 1, 1.02), (rii, -1, 0.85)):
                F.quad(at(rr * math.cos(th[i]), zs + rr * math.sin(th[i]), dep / 2),
                       at(rr * math.cos(th[i + 1]), zs + rr * math.sin(th[i + 1]), dep / 2),
                       at(rr * math.cos(th[i + 1]), zs + rr * math.sin(th[i + 1]), -dep / 2),
                       at(rr * math.cos(th[i]), zs + rr * math.sin(th[i]), -dep / 2), white * sh,
                       (d[0] * math.cos(xm) * sg, d[1] * math.cos(xm) * sg, math.sin(xm) * sg))


# The Chapiteau de Fontvieille: the permanent big top of the circus
# festival, a white membrane on four masts (20 cm photo: peaks north,
# east, south and west of the centre; LiDAR: 23 m at the masts, 21 m in the
# middle, ~11 m at the rim), a 60 m drum of side curtain; two small white
# pagoda tents by its south-west door.
CHAPITEAU = "BATIMENT0000000074079045"
FONTVIEILLE_PORT = box(1055850, 6302150, 1056160, 6302420)   # its basin (Lambert-93)
CHAP_C = (1055930.6, 6301882.4)
CHAP_R = 30.0
CHAP_EAVE = 10.8
CHAP_MAST_R = 16.0
CHAP_PEAK = 23.2
CHAP_SMALL = [((-37.0, -19.0), 9.0, 16.0), ((-27.0, -34.0), 9.5, 16.5), ((-35.0, -56.0), 8.5, 15.0)]


def _tent(F, s, t, R, z_ground, z_eave, masts, peak, centre_z, col, wall_col, n=64, rings=14):
    """A membrane tent: a drum of side curtain, then a surface from the rim
    up to the mast heads, sagging between them; radial seams catch the
    light with a slight scallop between them."""
    th = np.linspace(0, 2 * math.pi, n + 1)
    F.drum(s, t, z_ground, z_eave, R, wall_col, n=n)

    def zf(x, y):
        r = math.hypot(x - s, y - t)
        base = z_eave + (centre_z - z_eave) * max(0.0, 1 - r / R) ** 0.75
        bump = 0.0
        for ms, mt in masts:
            dd = math.hypot(x - ms, y - mt)
            rb = (peak - (z_eave + (centre_z - z_eave) * max(0.0, 1 - math.hypot(ms - s, mt - t) / R) ** 0.75))
            bump = max(bump, rb * max(0.0, 1 - dd / 9.0) ** 1.6)
        return base + bump

    rr = np.linspace(R, 0.0, rings + 1)
    grid = [[(s + r_ * math.cos(a), t + r_ * math.sin(a)) for a in th] for r_ in rr]
    Z = [[zf(*p) for p in row] for row in grid]
    for j in range(rings):
        for i in range(n):
            a = (*grid[j][i], Z[j][i])
            b = (*grid[j][i + 1], Z[j][i + 1])
            c = (*grid[j + 1][i + 1], Z[j + 1][i + 1])
            d = (*grid[j + 1][i], Z[j + 1][i])
            seam = 0.93 if i % 4 == 0 else 1.0
            xm = (th[i] + th[i + 1]) / 2
            F.tri(a, b, c, col * seam, (math.cos(xm) * 0.3, math.sin(xm) * 0.3, 1))
            if j < rings - 1 or True:
                F.tri(a, c, d, col * seam, (math.cos(xm) * 0.3, math.sin(xm) * 0.3, 1))


def chapiteau(m, mntR, stats):
    F = Frame(m, CHAP_C, (1.0, 0.0))
    th = np.linspace(0, 2 * math.pi, 25)
    zg = mntR.sample(CHAP_C[0] + (CHAP_R + 1.5) * np.cos(th), CHAP_C[1] + (CHAP_R + 1.5) * np.sin(th))
    zb = float(np.nanmin(zg)) - 0.3 if np.isfinite(zg).any() else 4.5
    white = np.array(hexrgb("#f3f2ee"), float)
    curtain = np.array(hexrgb("#e4e1da"), float)
    steel = np.array(hexrgb("#8d9196"), float)
    # Frame axes: s east, t south (Frame's v = (u_y, -u_x)).
    masts = [(0.0, -CHAP_MAST_R), (CHAP_MAST_R, 0.0), (0.0, CHAP_MAST_R), (-CHAP_MAST_R, 0.0)]
    _tent(F, 0.0, 0.0, CHAP_R, zb, CHAP_EAVE, masts, CHAP_PEAK, 21.0, white, curtain)
    for ms, mt in masts:
        x, y = F.xy(ms, mt)
        _cyl(m, x, y, CHAP_PEAK - 1.0, CHAP_PEAK + 3.2, 0.22, steel, 8)
        # A Monaco flag on each mast head.
        F.box(ms + 0.25, ms + 1.75, mt - 0.02, mt + 0.02, CHAP_PEAK + 2.4, CHAP_PEAK + 2.85,
              np.array(hexrgb("#ce1126"), float))
        F.box(ms + 0.25, ms + 1.75, mt - 0.02, mt + 0.02, CHAP_PEAK + 1.95, CHAP_PEAK + 2.4, white)
    # Entrance canopy on the south-west.
    for (ds, dt), r, pk in CHAP_SMALL:
        s_, t_ = ds, -dt
        _tent(F, s_, t_, r, zb, zb + 3.2, [(s_, t_)], pk, pk - 1.0, white, curtain, n=24, rings=6)
        x, y = F.xy(s_, t_)
        _cyl(m, x, y, pk - 0.5, pk + 1.5, 0.12, steel, 6)
    stats["landmarks"] = stats.get("landmarks", 0) + 1
    stats["chapiteau"] = 1
    pts = [F.xy(CHAP_R * math.cos(a), CHAP_R * math.sin(a)) for a in th[:-1]]
    return Polygon(pts)


# ------------------------------------------------------------- the Digue ----
# Port Hercule's semi-floating breakwater (2002, 352 m by 28 m), moored by a
# giant ball joint to its abutment at the foot of Fort Antoine, and that
# abutment's platform: a car park, a tree-lined walk and the Solarium's
# sunbathing decks stepping down to the sea. The LiDAR terrain model has no
# ground on the floating part (it is "sea"), so BD TOPO's two outlines on it
# (the cruise terminal, the raised walk) came out as blocks standing in the
# water, and the abutment was painted like sand with lawns (the photo's tents
# and the canopy finder). Built here from the IGN 20 cm photo (2023), the
# LiDAR surface model's levels (2021: harbour deck 3.0-3.1 m, raised walk
# 8.8 m by the terminal and 7.1 m beyond, terminal roof 14.1 m) and Mapillary
# photos taken from Fort Antoine and the abutment (2017-2021): a long pale
# concrete deck with a dark asphalt lane on the harbour side, a high pale
# walk along the sea side behind a parapet, the terminal's red-brown block
# over the lane, a scalloped lower walk at the sea's edge and the head's
# low white building with the port light.
# Frame: s along the breakwater from the abutment (m), t across, out to sea
# (south-east) positive; the harbour face is at t = DIGUE_HARBOUR.
DIGUE_O = (1056645.46, 6302662.51)
DIGUE_U = (280.54, 382.80)
DIGUE_PARTS = ("BATIMENT0000002009026510", "BATIMENT0000002009010575",
               "BATIMENT0000000074074333", "BATIMENT0000002009009578")
DIGUE_DONE = []
DIGUE_ROOT, DIGUE_END = 126.0, 474.6     # s: the floating deck's start (the ball joint) and the head
DIGUE_HARBOUR = -28.5                    # t of the harbour face (the sea face is t = 0)
DIGUE_DECK = 3.1                         # m NGF: the lane, the quay edge and the lower sea walk
DIGUE_WALK_HI = 8.8                      # the raised walk from the root to the terminal's end
DIGUE_WALK_LO = 7.1                      # ... and beyond, toward the head
DIGUE_STEP_S = 330.0                     # s where the raised walk steps down
DIGUE_TERMINAL = (215.0, 322.0, -22.0, -8.0, 14.1)
DIGUE_CONCRETE = "#d7d2c7"               # the deck's pale concrete
DIGUE_WALL = "#c9c3b6"                   # its faces
DIGUE_ASPHALT = "#5f6266"
DIGUE_TERMINAL_COL = "#a9533f"           # red-brown cladding (Mapillary 2017)
DIGUE_TERMINAL_ROOF = "#8e5446"
# The abutment, in the same frame: (s0, s1, t0, t1) boxes of what the photo
# shows. Levels from the LiDAR terrain model.
DIGUE_PAINT = [
    ((22.0, 140.0, -95.0, -11.0), "#6d6f72"),      # the car park, the boat yard and the Quai Rainier loop's asphalt
    ((20.0, 40.0, -20.0, -9.0), "#9b6a58"),        # the terrace with round planters under the Fort
    ((-4.0, 144.0, -9.0, 15.0), "#a9a7a1"),       # the Solarium (under its concrete, see solarium())
]

# The Solarium ("Plage du Solarium"), the public bathing terraces along the
# abutment's sea side under Fort Antoine. OpenStreetMap maps it as a beach
# (natural=beach, surface=asphalt) and the photo's pale concrete read as
# sand: it was three cream slabs on the 3 m terrain grid. What is there is a
# concrete grandstand into the sea. The LiDAR surface model (2021) shows it
# plainly: a top terrace at 7.0 m NGF, then six tiers 2 m deep and 1 m high
# down to a last one at 1.0 m and the sea wall (t 14.8); behind it, a raised
# concrete promenade on a two-level structure (OSM building=pier, footway
# level 2, surface concrete) from 11.0-11.5 m under the Fort down to 8.8 m
# where it runs on as the breakwater's raised walk, with a wall into the
# car park (2 m) on the harbour side. At the north end the tiers stop at a
# platform at 3.0 m reached by a flight from the terrace, at the south end
# a platform at 4.9 m against the Fort's rock. Benches, showers and the
# drinking tap are OpenStreetMap's (2023 survey); ladders into the sea as
# the bathing guides describe.
SOL_TOP = 7.0                            # m NGF: the top terrace
SOL_T0 = -0.8                            # t: the promenade's sea wall (the terrace's back)
SOL_EDGE = 2.8                           # t: the first tier's edge; then one every SOL_STEP
SOL_STEP = 2.0
SOL_TIERS = 6                            # 6.0 .. 1.0 m
SOL_SEA = SOL_EDGE + SOL_STEP * SOL_TIERS   # 14.8: the sea wall
SOL_S0, SOL_S1 = 8.0, 122.0              # the top terrace along s
SOL_NORTH = (126.0, 139.0, 3.0)          # the north platform: from, to, level
SOL_LEDGE = (139.0, 142.5, 1.2)          # the low ledge by the breakwater's root
SOL_SOUTH = (-4.0, 8.0, -2.5, 4.9)       # the south platform: s0, s1, t0, level
SOL_PROM = [(-4.0, 11.0), (15.0, 11.0), (30.0, 11.5), (42.0, 11.5), (50.0, 11.1), (62.0, 10.75),
            (80.0, 10.2), (100.0, 9.65), (119.0, 9.1), (131.0, 8.9), (140.0, 8.8)]   # (s, level) along t -4
SOL_PROM_T = -9.0                        # t: the promenade's harbour wall
SOL_CARPARK = 1.6                        # the harbour wall's foot (car park 2.0)
SOL_CONC = "#a9aaa7"                     # sun-bleached concrete, cool grey (the grade warms it)
SOL_RISER = "#8a8b88"                    # the tiers' faces, a shade darker so each step reads
SOL_SEAWALL = "#7c7d7a"
SOL_WET = "#4f5552"
SOL_PROM_COL = "#a4a29d"
SOL_WALL = "#b4b1aa"
SOL_BENCHES = [(43.6, 0.6), (45.7, 0.6), (55.1, 0.6), (57.2, 0.6), (59.7, 0.6), (70.4, 0.8), (72.5, 0.8),
               (75.0, 0.8), (85.9, 0.6), (88.4, 0.6), (49.4, 3.7), (50.9, 3.6), (80.0, 3.7), (81.5, 3.6),
               (10.6, 5.2), (17.7, 4.7)]
SOL_SHOWERS = [(36.2, 0.5), (65.4, 0.6), (83.7, 0.7)]
SOL_LADDERS = [15.0, 31.0, 47.0, 70.0, 93.0, 110.0, 133.0]
SOL_FLIGHTS = [24.0, 62.0, 101.0]        # stairs down through the tiers
DIGUE_BAYS = [((-30.5, -25.5), (60.0, 100.0)), ((-25.5, -20.5), (60.0, 100.0))]


def digue_frame(m=None):
    return Frame(m, DIGUE_O, DIGUE_U)


def digue_zone():
    """The breakwater and the abutment's decks (no LiDAR trees, no lawn)."""
    F = digue_frame()
    return unary_union([F.poly(DIGUE_ROOT - 2.0, DIGUE_END + 1.0, DIGUE_HARBOUR - 0.5, 0.5),
                        F.poly(26.0, 142.0, -11.0, 15.0), F.poly(-5.0, 146.0, -12.5, 16.0)])


def sol_prom_z(s):
    """The Solarium's promenade level at s."""
    a = np.array(SOL_PROM)
    return np.interp(s, a[:, 0], a[:, 1])


def solarium_ground(mnt):
    """The 3 m ground grid under the Solarium goes under water: the tiers
    stand alone, with no terrain slope cutting through their edges."""
    F = digue_frame()
    P = F.poly(SOL_SOUTH[0] + 1.0, SOL_LEDGE[1] + 2.0, SOL_PROM_T + 0.6, SOL_SEA + 1.5)
    x0, y0, x1, y1 = P.bounds
    c0, c1 = max(int((x0 - X0) / RES), 0), min(int((x1 - X0) / RES) + 1, mnt.shape[1])
    r0, r1 = max(int((Y1 - y1) / RES), 0), min(int((Y1 - y0) / RES) + 1, mnt.shape[0])
    rr, cc = np.mgrid[r0:r1, c0:c1]
    cx, cy = cell_xy(rr, cc)
    k = contains_xy(P, cx, cy)
    out = mnt.copy()
    sub = out[r0:r1, c0:c1]
    sub[k] = -0.6
    out[r0:r1, c0:c1] = sub
    return out


def digue_paint(poly, line):
    """The abutment's ground map (paint_ground's poly and line helpers)."""
    F = digue_frame()
    for (s0, s1, t0, t1), col in DIGUE_PAINT:
        poly(np.asarray(F.poly(s0, s1, t0, t1).exterior.coords), col)
    # The car park's bays: white lines across two rows (20 cm photo).
    for (t0, t1), (sa, sb) in DIGUE_BAYS:
        for s in np.arange(sa, sb + 0.1, 2.5):
            line([F.xy(s, t0), F.xy(s, t1)], "#c4c3be", 0.15)


def digue(m, tm, mntR, stats):
    F = digue_frame(m)
    C = lambda h: np.array(hexrgb(h), float)
    conc, wall, asph = C(DIGUE_CONCRETE), C(DIGUE_WALL), C(DIGUE_ASPHALT)
    r0, r1, th, d = DIGUE_ROOT, DIGUE_END, DIGUE_HARBOUR, DIGUE_DECK
    ts0, ts1, tt0, tt1, troof = DIGUE_TERMINAL
    bottom = -2.0
    # The hull: plain concrete faces into the water, a darker wet band at
    # the waterline, and the deck. The lane is asphalt between a pale quay
    # edge (with bollards) and the raised walk's wall.
    F.box(r0, r1, th, 0.0, bottom, d, wall, top=False, sides="tTS")
    F.box(r0, r1, th - 0.02, 0.02, 0.2, 1.1, wall * 0.72, top=False, sides="tTS")
    F.box(r0, r1, th, th + 1.6, d, d + 0.06, conc)                                  # quay edge
    F.box(r0, r1, th + 1.6, -12.0, d, d + 0.03, asph)                               # the lane
    for s in np.arange(r0 + 6.0, r1 - 4.0, 12.0):
        _cyl(m, *F.xy(s, th + 0.5), d, d + 0.55, 0.22, C("#2f3133"), sides=8)       # bollards
    # A white dashed line down the lane.
    for s in np.arange(r0 + 4.0, r1 - 12.0, 9.0):
        F.box(s, s + 4.5, -19.9, -19.75, d + 0.03, d + 0.05, C("#e4e2dc"), sides="")
    # The raised walk along the sea side: 8.8 m from the root to the
    # terminal's end, then 7.1 m toward the head behind a lower sea walk
    # (3.1 m) with its scalloped edge (the wave chambers' curved tops).
    lo_t0 = -4.5
    hi = [(r0 + 14.0, ts0, -12.0, 0.0), (ts0, ts1, tt1, 0.0), (ts1, DIGUE_STEP_S, -14.0, 0.0)]
    for s0, s1, t0, t1 in hi:
        F.box(s0, s1, t0, t1, d, DIGUE_WALK_HI, conc, sides="tT")     # the Solarium's promenade runs on at the root
    F.box(DIGUE_STEP_S, 440.0, -12.0, lo_t0, d, DIGUE_WALK_LO, conc, sides="tTS")
    F.box(DIGUE_STEP_S, DIGUE_STEP_S + 0.01, -14.0, lo_t0, DIGUE_WALK_LO, DIGUE_WALK_HI, conc, top=False, sides="S")
    F.box(DIGUE_STEP_S, r1, lo_t0, 0.0, d, d + 0.05, conc * 0.98)                    # the lower sea walk
    for s in np.arange(DIGUE_STEP_S + 4.0, r1 - 6.0, 7.4):
        F.drum(s, 0.0, d, d + 0.55, 3.1, conc * 0.93, n=10, th0=math.pi, th1=2 * math.pi)
        F.cone(s, 0.0, d + 0.55, 3.1, 0.25, conc * 0.97, n=10, th0=math.pi, th1=2 * math.pi, kind=PLAIN)
    # Steps from the raised walk down to the lower one by the terminal's end
    # (at the root the walk runs on as the Solarium's promenade).
    _stairs(m, F.xy(DIGUE_STEP_S + 0.5, -2.2), F.xy(DIGUE_STEP_S + 9.0, -2.2), DIGUE_WALK_HI, d, 3.6, conc, wall)
    # Parapets: a pale wall on the sea edge of the high walk, white
    # railings along its harbour side and round the lower walk.
    F.box(r0 + 14.0, DIGUE_STEP_S, -0.6, 0.0, DIGUE_WALK_HI, DIGUE_WALK_HI + 1.1, conc * 1.02)
    for s0, s1, t_, z in ((r0 + 14.0, ts0, -12.0, DIGUE_WALK_HI), (ts1, DIGUE_STEP_S, -14.0, DIGUE_WALK_HI),
                          (DIGUE_STEP_S, 440.0, -12.0, DIGUE_WALK_LO), (DIGUE_STEP_S + 9.0, 440.0, lo_t0, DIGUE_WALK_LO)):
        railing(m, np.array([F.xy(s0, t_ + 0.1)]), np.array([F.xy(s1, t_ + 0.1)]), z, STREET_RAIL)
    # Lamps along the high walk.
    for s in np.arange(r0 + 22.0, 440.0, 24.0):
        t_ = -1.2 if s < DIGUE_STEP_S else -5.5
        z = DIGUE_WALK_HI if s < DIGUE_STEP_S else DIGUE_WALK_LO
        _cyl(m, *F.xy(s, t_), z, z + 4.2, 0.07, C("#3a3d3f"), sides=6)
        F.box(s - 0.3, s + 0.3, t_ - 0.2, t_ + 0.2, z + 4.1, z + 4.35, C("#3a3d3f"))
    # The cruise terminal: a long block over the lane, red-brown cladding
    # with a band of glass on each floor, a flat brown roof.
    col = C(DIGUE_TERMINAL_COL)
    tpoly = F.poly(ts0, ts1, tt0, tt1)
    add_walls(m, tpoly, d, troof - 0.9, d, 3.6, col, WALL_NEW, style=S_RIBBON)
    F.box(ts0 - 0.3, ts1 + 0.3, tt0 - 0.3, tt1 + 0.3, troof - 0.9, troof, col * 0.9)
    add_roof(m, tpoly, troof, C(DIGUE_TERMINAL_ROOF), False)
    for s in np.arange(ts0 + 8.0, ts1 - 6.0, 18.0):
        F.box(s, s + 6.0, tt0 + 3.0, tt0 + 7.0, troof, troof + 1.6, C("#b9b5ad"))      # roof plant
    # The head: a pale platform round a low white building and the port's
    # light, a white tower with a red lantern (the harbour's red side).
    F.box(440.0, r1, th + 1.6, lo_t0, d, d + 0.35, conc)
    add_walls(m, F.poly(452.0, 469.0, -24.0, -9.0), d + 0.35, d + 6.6, d + 0.35, 3.1, C("#efeeea"), WALL_NEW,
              style=S_MODERN)
    F.box(451.5, 469.5, -24.5, -8.5, d + 6.6, d + 7.0, C("#dcdad3"))
    F.box(466.0, 467.0, -22.0, -21.0, d + 7.0, d + 13.0, C("#9a9c9e"))                 # the mast
    F.drum(r1 - 2.6, th + 2.6, d + 0.35, d + 9.0, 1.1, C("#f4f2ee"), n=10)
    F.drum(r1 - 2.6, th + 2.6, d + 9.0, d + 10.6, 0.8, C("#c3352c"), n=10)
    F.cone(r1 - 2.6, th + 2.6, d + 10.6, 1.0, 0.8, C("#7c2a24"), n=10, kind=PLAIN)
    # The root: the deck runs onto the abutment over a short ramp (the car
    # park is at 2 m); on its sea side, the Solarium.
    F.box(r0 - 8.0, r0, th + 1.6, -12.0, 1.9, 2.0, asph, sides="")
    F.quad((r0 - 8.0, th + 1.6, 2.03), (r0, th + 1.6, d + 0.03), (r0, -12.0, d + 0.03), (r0 - 8.0, -12.0, 2.03), asph,
           (0, 0, 1))
    nt = solarium(m, tm, stats)
    stats["digue_m"] = round(r1 - r0, 1)
    stats["digue_trees"] = nt
    return nt


def solarium(m, tm, stats):
    """The Solarium: the concrete tiers into the sea, the top terrace, the
    raised promenade behind it, the platforms at both ends, ladders,
    flights of steps, benches, showers and the promenade's small trees."""
    F = digue_frame(m)
    C = lambda h: np.array(hexrgb(h), float)
    conc, riser, seaw, wet = C(SOL_CONC), C(SOL_RISER), C(SOL_SEAWALL), C(SOL_WET)
    pcol, wall = C(SOL_PROM_COL), C(SOL_WALL)
    steel, wood = C("#c4c8cb"), C("#8b5d3b")
    foot = -1.5
    # Cast concrete in bays: each 6 m slab a shade apart, so the joints show.
    shade = lambda s, t: 0.975 + 0.05 * int(hashlib.md5(f"sol{s:.1f}:{t:.1f}".encode()).hexdigest()[:6], 16) / 0xFFFFFF

    def tread(s0, s1, t0, t1, z, sides="T", face=riser, bay=6.0):
        n = max(1, int(round((s1 - s0) / bay)))
        e = np.linspace(s0, s1, n + 1)
        for a, b in zip(e[:-1], e[1:]):
            F.box(a, b, t0, t1, z - 0.02, z, conc * shade(a, t0), sides="")
        if sides:
            F.box(s0, s1, t0, t1, foot, z, face, top=False, sides=sides)

    # The top terrace and the six tiers, the last one standing on the sea
    # wall; their ends step down to the platforms.
    tread(SOL_S0, SOL_S1, SOL_T0, SOL_EDGE, SOL_TOP, sides="sST")
    for k in range(1, SOL_TIERS + 1):
        t0, t1, z = SOL_EDGE + SOL_STEP * (k - 1), SOL_EDGE + SOL_STEP * k, SOL_TOP - k
        s0 = SOL_S0 if z > SOL_SOUTH[3] - 0.5 else SOL_SOUTH[0] + 1.0
        s1 = SOL_NORTH[0] if z > SOL_NORTH[2] + 0.5 else SOL_NORTH[1]
        tread(s0, s1, t0, t1, z, sides="sST" if k < SOL_TIERS else "sS")
    # The sea wall: plain concrete, darker and wet at the waterline.
    F.box(SOL_SOUTH[0] + 1.0, SOL_NORTH[1], SOL_SEA - 0.01, SOL_SEA, -2.5, SOL_TOP - SOL_TIERS, seaw, top=False, sides="T")
    F.box(SOL_SOUTH[0] + 1.0, SOL_LEDGE[1], SOL_SEA, SOL_SEA + 0.02, -0.6, 0.35, wet, top=False, sides="T")
    # North: a platform at 3.0 m and the low ledge at the breakwater's root,
    # with a flight down from the top terrace along the promenade's wall.
    ns0, ns1, nz = SOL_NORTH
    tread(ns0, ns1, SOL_T0, SOL_EDGE + SOL_STEP * 3, nz, sides="")
    ls0, ls1, lz = SOL_LEDGE
    tread(ls0, ls1, SOL_T0, SOL_SEA, lz, sides="tTS")
    F.box(ns1, ls0 + 0.01, SOL_T0, SOL_EDGE + SOL_STEP * 4, lz, nz, riser, top=False, sides="S")
    _stairs(m, F.xy(SOL_S1, (SOL_T0 + SOL_EDGE) / 2), F.xy(SOL_S1 + 11.0, (SOL_T0 + SOL_EDGE) / 2), SOL_TOP, nz,
            SOL_EDGE - SOL_T0 - 0.6, conc, riser)
    # South: a platform at 4.9 m against the Fort's rock, a flight down to it.
    ss0, ss1, st0, sz = SOL_SOUTH
    tread(ss0, ss1, st0, SOL_EDGE + SOL_STEP * 2, sz, sides="stT")
    _stairs(m, F.xy(SOL_S0, (SOL_T0 + SOL_EDGE) / 2), F.xy(SOL_S0 - 5.0, (SOL_T0 + SOL_EDGE) / 2), SOL_TOP, sz,
            SOL_EDGE - SOL_T0 - 0.6, conc, riser)
    # Flights of steps down through the tiers to the water (0.2 m steps).
    for sc in SOL_FLIGHTS:
        n = int(round((SOL_TOP - (SOL_TOP - SOL_TIERS)) / 0.2))
        dt = (SOL_SEA - 0.4 - SOL_EDGE) / n
        for i in range(n):
            ta = SOL_EDGE + dt * i
            F.box(sc - 0.8, sc + 0.8, ta, ta + dt + 0.01, foot, SOL_TOP - 0.2 * (i + 1), conc * 0.97, sides="sST")
        for side in (-0.85, 0.85):                                         # steel handrails
            for i in range(0, n, 5):
                ta = SOL_EDGE + dt * i
                F.box(sc + side - 0.03, sc + side + 0.03, ta, ta + 0.06, SOL_TOP - 0.2 * (i + 1),
                      SOL_TOP - 0.2 * (i + 1) + 0.95, steel)
            for i in range(n - 1):
                ta = SOL_EDGE + dt * i
                F.box(sc + side - 0.03, sc + side + 0.03, ta, ta + dt + 0.01, SOL_TOP - 0.2 * (i + 1) + 0.9,
                      SOL_TOP - 0.2 * (i + 1) + 0.96, steel, sides="sS")
    # Ladders down the sea wall: two rails bent over the edge, rungs.
    for sc in SOL_LADDERS:
        z = SOL_TOP - SOL_TIERS if sc < ns1 else lz
        for side in (-0.28, 0.28):
            F.box(sc + side - 0.025, sc + side + 0.025, SOL_SEA + 0.12, SOL_SEA + 0.17, -1.4, z + 0.85, steel)
            F.box(sc + side - 0.025, sc + side + 0.025, SOL_SEA - 0.55, SOL_SEA + 0.17, z + 0.8, z + 0.85, steel)
            F.box(sc + side - 0.025, sc + side + 0.025, SOL_SEA - 0.55, SOL_SEA - 0.5, z, z + 0.85, steel)
        for zr in np.arange(-1.2, z, 0.3):
            F.box(sc - 0.28, sc + 0.28, SOL_SEA + 0.12, SOL_SEA + 0.16, zr, zr + 0.04, steel, sides="tT")
    # The promenade: a concrete deck on its structure, sloping with the
    # LiDAR, a wall into the car park and a parapet over the terrace.
    # Each 2.5 m piece is a sloping quad (no steps or gaps between them).
    def sloped(a, b, t0, t1, z0, z1, col, sides, top=True):
        za0, zb0, za1, zb1 = z0(a), z0(b), z1(a), z1(b)
        if top:
            F.quad((a, t0, za1), (b, t0, zb1), (b, t1, zb1), (a, t1, za1), col * 1.02, (0, 0, 1))
        for c in sides:
            tt = t0 if c == "t" else t1
            F.quad((a, tt, za0), (b, tt, zb0), (b, tt, zb1), (a, tt, za1), col, (0, -1 if c == "t" else 1, 0))

    e = np.arange(SOL_SOUTH[0], 140.0 + 0.01, 2.5)
    zp = lambda x: float(sol_prom_z(x))
    for a, b in zip(e[:-1], e[1:]):
        t1 = SOL_T0 if a >= SOL_S0 - 2.0 else SOL_SOUTH[2]
        t0 = SOL_PROM_T if a < 125.0 else SOL_PROM_T - 3.0 * (a - 125.0) / 15.0
        sloped(a, b, t0, t1, lambda x: SOL_CARPARK, zp, wall, "tT", top=False)
        F.quad((a, t0, zp(a)), (b, t0, zp(b)), (b, t1, zp(b)), (a, t1, zp(a)), pcol * shade(a, -5), (0, 0, 1))
        sloped(a, b, t1 - 0.4, t1, zp, lambda x: zp(x) + 1.0, wall * 1.03, "tT")
        if a == SOL_S0 - 2.0 or a == SOL_SOUTH[0]:
            F.box(a - 0.01, a, t1 - 0.4, t1, zp(a), zp(a) + 1.0, wall * 1.03, top=False, sides="s")
    # The south end's face toward the Fort and the north end's join with the
    # breakwater's raised walk.
    F.box(SOL_SOUTH[0], SOL_SOUTH[0] + 0.01, SOL_PROM_T, SOL_SOUTH[2], SOL_CARPARK, float(sol_prom_z(SOL_SOUTH[0])), wall,
          top=False, sides="s")
    # A railing along the harbour wall over the car park (OSM: from s 46).
    A = np.array([F.xy(a, SOL_PROM_T + 0.1) for a in e[18:-6]])
    B = np.array([F.xy(b, SOL_PROM_T + 0.1) for b in e[19:-5]])
    railing(m, A, B, sol_prom_z(e[18:-6]), STREET_RAIL, zb=sol_prom_z(e[19:-5]))
    # Two flights from the promenade down to the terrace, meeting at s 56.5.
    for a, b in ((46.0, 56.0), (67.0, 57.0)):
        _stairs(m, F.xy(a, 0.0), F.xy(b, 0.0), float(sol_prom_z(a)), SOL_TOP, 1.6, conc, wall)
    # Benches (wooden slats on concrete feet), facing the sea.
    for sb, tb in SOL_BENCHES:
        k = int(np.clip(np.floor((tb - SOL_EDGE) / SOL_STEP) + 1, 0, SOL_TIERS))
        z = SOL_TOP - k
        F.box(sb - 0.8, sb + 0.8, tb - 0.25, tb + 0.25, z + 0.38, z + 0.45, wood)
        F.box(sb - 0.8, sb + 0.8, tb - 0.3, tb - 0.24, z + 0.45, z + 0.9, wood)
        for o in (-0.65, 0.65):
            F.box(sb + o - 0.06, sb + o + 0.06, tb - 0.25, tb + 0.2, z, z + 0.38, riser)
    # Showers and the drinking tap: steel posts.
    for sb, tb in SOL_SHOWERS:
        x, y = F.xy(sb, tb)
        _cyl(m, x, y, SOL_TOP, SOL_TOP + 2.3, 0.05, steel, 8)
        F.box(sb - 0.05, sb + 0.05, tb, tb + 0.35, SOL_TOP + 2.22, SOL_TOP + 2.3, steel)
        F.box(sb - 0.4, sb + 0.4, tb - 0.4, tb + 0.4, SOL_TOP, SOL_TOP + 0.03, riser * 0.85)
    x, y = F.xy(19.1, -0.6)
    _cyl(m, x, y, SOL_TOP, SOL_TOP + 1.0, 0.12, steel, 8)
    # The promenade's small trees in square planters (the 20 cm photo).
    rnd = rng_for("digue-trees")
    nt = 0
    for s in np.arange(9.0, 136.0, 8.5):
        sc = s + rnd.uniform(-1, 1)
        z = float(sol_prom_z(sc))
        F.box(sc - 0.7, sc + 0.7, -3.1, -1.7, z, z + 0.5, wall * 0.97)
        x, y = F.xy(sc, -2.4)
        add_tree(tm, x, y, z + 0.5, rnd.uniform(4.0, 5.5), rnd.uniform(1.5, 2.0), "broad", rnd)
        nt += 1
    stats["solarium_tiers"] = SOL_TIERS
    stats["solarium_m"] = round(SOL_NORTH[1] - SOL_SOUTH[0], 1)
    return nt


def digue_footprint():
    """The breakwater's plan, for the building mask (no trees or lawn on it)."""
    F = digue_frame()
    return orient(F.poly(DIGUE_ROOT, DIGUE_END, DIGUE_HARBOUR, 0.0), 1.0)


# The Centre Commercial de Fontvieille (Carrefour and its mall): a podium
# from the streets up to its roof, the Esplanade Rainier III and the
# Jardin de l'Unesco, which the LiDAR terrain model takes for ground (19 m
# NGF over streets at 5 m). The 3 m ground grid ran that drop out as steep
# pale slopes. Its outline (OpenStreetMap) gets white walls with tall
# arched windows (Mapillary 2021: the Avenue Albert II entrance), the
# ground nodes either side of each drop go down to the street, and a strip
# of the esplanade is drawn level on top, with its white railing.
PODIUM_NAMES = ("Centre Commercial Fontvieille",)
# Mapped as gardens, but paved squares (Mapillary 2019: red and grey brick
# on the shopping centre's roof): the photo shows through, not a lawn.
PAVED_GARDENS = ("Esplanade Rainier III",)
PODIUM_WALL = "#f0ebe1"
PODIUM_CAP = 2 * GRID + 0.6            # m of deck drawn level inside the edge
PODIUMS = []


def podium_setup(ways, mnt):
    PODIUMS.clear()
    R = Raster(mnt)
    for t, p_, closed in ways:
        if not closed or t.get("name") not in PODIUM_NAMES or len(p_) < 4:
            continue
        L = Polygon(p_).buffer(0)
        if L.area < 500:
            continue
        # The deck's real edge: where the terrain model stands 5 m or more
        # over the streets round it (the mapped outline runs along the
        # street fronts of the blocks, not the drops).
        x0, y0, x1, y1 = L.buffer(10).bounds
        c0, c1 = max(int((x0 - X0) / RES), 0), min(int((x1 - X0) / RES) + 1, mnt.shape[1])
        r0, r1 = max(int((Y1 - y1) / RES), 0), min(int((Y1 - y0) / RES) + 1, mnt.shape[0])
        sub = np.nan_to_num(mnt[r0:r1, c0:c1], nan=0.0)
        street = float(np.nanpercentile(R.sample(*np.asarray(L.buffer(8).exterior.coords)[:, :2].T), 10))
        rr_, cc_ = np.mgrid[r0:r1, c0:c1]
        cx_, cy_ = cell_xy(rr_, cc_)
        mk = (sub > street + 5.0) & contains_xy(L.buffer(8), cx_, cy_)
        mk = ndimage.binary_opening(ndimage.binary_closing(mk, iterations=2), iterations=2)
        lab, nl = ndimage.label(mk)
        if not nl:
            continue
        big = np.argmax(ndimage.sum(mk, lab, range(1, nl + 1))) + 1
        rs_, cs_ = np.nonzero(lab == big)
        bx_, by_ = cell_xy(rs_ + r0, cs_ + c0)
        P = unary_union([box(a - RES / 2, b - RES / 2, a + RES / 2, b + RES / 2) for a, b in zip(bx_, by_)])
        P = max(to_polys(P.buffer(0.3).buffer(-0.3).simplify(0.8)), key=lambda q: q.area)
        P = orient(Polygon(P.exterior), 1.0)
        ring = P.exterior
        n = max(8, int(ring.length / 1.0))
        s = np.linspace(0, ring.length, n + 1)
        q = shapely.line_interpolate_point(ring, s)
        x, y = shapely.get_x(q), shapely.get_y(q)
        d = np.gradient(np.column_stack([x, y]), axis=0)
        d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
        o = np.column_stack([d[:, 1], -d[:, 0]])              # outward (counter-clockwise ring)
        zin = np.nanmedian(np.stack([R.sample(x - o[:, 0] * k, y - o[:, 1] * k) for k in (1.5, 2.5, 3.5)]), 0)
        zout = np.nanmin(np.stack([R.sample(x + o[:, 0] * k, y + o[:, 1] * k) for k in (2.0, 3.5, 5.0)]), 0)
        drop = np.isfinite(zin) & np.isfinite(zout) & (zin - zout > 2.5)
        # Smooth the top along the edge (a 1 m sampling of a 50 cm scan).
        zin = ndimage.median_filter(np.nan_to_num(zin, nan=0.0), 5, mode="wrap")
        zout = ndimage.minimum_filter1d(np.nan_to_num(zout, nan=0.0), 5, mode="wrap")
        drop = ndimage.binary_opening(drop, iterations=2) | (drop & ndimage.binary_dilation(drop, iterations=2))
        PODIUMS.append({"poly": P, "x": x, "y": y, "o": o, "zin": zin, "zout": zout, "drop": drop})
    return len(PODIUMS)


def podium_ground(mnt):
    """The terrain model for the ground mesh: nodes within a grid step of a
    podium's drop, on both sides, at the street's level."""
    if not PODIUMS:
        return mnt
    from scipy.spatial import cKDTree
    out = mnt.copy()
    for pd in PODIUMS:
        k = pd["drop"]
        if not k.any():
            continue
        x0, y0, x1, y1 = pd["poly"].buffer(GRID + 4).bounds
        c0, c1 = max(int((x0 - X0) / RES), 0), min(int((x1 - X0) / RES) + 1, mnt.shape[1])
        r0, r1 = max(int((Y1 - y1) / RES), 0), min(int((Y1 - y0) / RES) + 1, mnt.shape[0])
        rr, cc = np.mgrid[r0:r1, c0:c1]
        cx, cy = cell_xy(rr.ravel(), cc.ravel())
        kd = cKDTree(np.column_stack([pd["x"][k], pd["y"][k]]))
        dd, j = kd.query(np.column_stack([cx, cy]), distance_upper_bound=GRID + 2.6)
        hit = np.isfinite(dd)
        # Outside: up to a grid step and a bit; inside: the band under the cap.
        t = ((cx - pd["x"][k][np.where(hit, j, 0)]) * pd["o"][k][np.where(hit, j, 0), 0]
             + (cy - pd["y"][k][np.where(hit, j, 0)]) * pd["o"][k][np.where(hit, j, 0), 1])
        sel = hit & (t > -(GRID + 1.6))
        zo = pd["zout"][k][np.where(hit, j, 0)]
        sub = out[r0:r1, c0:c1].ravel()
        sub[sel] = np.minimum(np.nan_to_num(sub[sel], nan=99.0), zo[sel] - 0.1)
        out[r0:r1, c0:c1] = sub.reshape(rr.shape)
    return out


def podium_walls(gm, m, stats):
    wall = np.array(hexrgb(PODIUM_WALL), float)
    n_ = 0
    for pd in PODIUMS:
        k = pd["drop"]
        idx = np.nonzero(k[:-1] & k[1:])[0]
        if not len(idx):
            continue
        for r_ in np.split(idx, np.nonzero(np.diff(idx) > 1)[0] + 1):
            j = np.r_[r_, r_[-1] + 1]
            x, y, o = pd["x"][j], pd["y"][j], pd["o"][j]
            zb, zt = pd["zout"][j] - 0.3, pd["zin"][j]
            # Arched shop windows, storeys of 5.2 m from the street.
            _strip_wall(m, x, y, zb, zt - 1.0, wall, WALL_OLD, S_ARCH, 5.2, 4.6)
            _strip_wall(m, x, y, zt - 1.0, zt + 0.05, wall * 1.03)
            # The esplanade's edge: level paving painted from the ground map.
            A = np.column_stack([x, y])
            Bc = A - o * PODIUM_CAP
            for a, b, c, d_, za, zb_ in zip(A[:-1] - o[:-1] * 0.05, A[1:] - o[1:] * 0.05, Bc[1:], Bc[:-1], zt[:-1], zt[1:]):
                V = np.array([a, b, c, d_])
                Z = np.array([za, zb_, zb_, za]) + 0.04
                P = enu_fast(V[:, 0], V[:, 1], Z)
                idx_ = np.array([[0, 1, 2], [0, 2, 3]])
                e1, e2 = P[idx_[:, 1]] - P[idx_[:, 0]], P[idx_[:, 2]] - P[idx_[:, 0]]
                fl = np.cross(e1, e2)[:, 2] < 0
                idx_[fl] = idx_[fl][:, [0, 2, 1]]
                uv = np.column_stack([(V[:, 0] - X0) / (X1 - X0), (Y1 - V[:, 1]) / (Y1 - Y0)])
                gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=idx_)
            # White railing along the top.
            Ar = A - o * 0.2
            railing(m, Ar[:-1], Ar[1:], zt[:-1], "#f4f2ec", 1.05, zt[1:])
            n_ += len(r_)
    stats["podium_wall_m"] = n_
    return n_


# ------------------------------------------------------ west harbours ----
# Port de Fontvieille and Port de Cap d'Ail (the French marina next door).
# Their quays are low (1.4-2 m NGF) and end in vertical walls; the long
# breakwaters run on two levels: a raised promenade at the heliport's level
# (5.7 m) on the sea side, over a lower quay at the water (LiDAR surface
# model: the promenade is a structure, so the terrain model has only the
# lower level, or nothing). The 3 m ground grid ran all of it as soft
# slopes into the sea, the painted ground took the 20 cm photo's parked
# cars, boats on the hard standing and awnings for lawn, and the yard's
# travel lift and boats stood up as grey mounds. Here, from the LiDAR and
# the 20 cm photo (2026-11-24):
# - Cap d'Ail's outer breakwater, in the heliport's own frame (s along the
#   apron's axis, t out to sea): a lower quay (road, parking along the
#   wall) at 1.95 m from t 0.8 to 9.8, the raised walk at 5.75 m from 9.8
#   to 16.4, its sea face straight into the water; past s -152 the walk
#   steps in (t 3 to 10) with a lower quay at 1.5 m outside it, which runs
#   on under the heliport's nets.
# - Fontvieille's seawall from the heliport to the outer jetty (its own
#   frame, axis 38 degrees): the rose-brown promenade at 5.7-6.5 m over a
#   lower quay at 1.4 m.
# - Every other quay edge in the two ports: a level deck by the water with
#   a vertical wall and a pale coping, except where the photo shows rock
#   armour (the curved bay, the Cap d'Ail mole's sea face).
# - Clean paving on the quays, the boatyard's hard standing in grey, and
#   the tall palms along the quays that the tree finder dropped for not
#   being green in the photo.
WQ = None
WQ_ZONES = [box(1055470.0, 6301500.0, 1055990.0, 6301870.0),     # Port de Cap d'Ail
            box(1055780.0, 6301880.0, 1056345.0, 6302380.0).difference(
                box(1056242.0, 6302150.0, 1056400.0, 6302400.0))]   # Port de Fontvieille (not the Rock's foot)
WQ_RIPRAP = [  # rock armour (20 cm photo), as lines along the shore
    [(1055470, 6301730), (1055492, 6301715), (1055525, 6301707), (1055565, 6301697), (1055610, 6301684),
     (1055660, 6301664), (1055695, 6301641), (1055708, 6301620)],
    [(1056117, 6302152), (1056122, 6302100), (1056140, 6302065), (1056165, 6302040), (1056195, 6302027),
     (1056228, 6302022)],
    [(1056236, 6302255), (1056236, 6302148)]]
WQ_DROP = ("BATIMENT0000000327487874",)                            # BD TOPO's "building" for the walk
WQ_YARD = box(1055500.0, 6301712.0, 1055562.0, 6301775.0)          # the boatyard's hard standing
WQ_FV_O, WQ_FV_AXIS = (1056075.0, 6301905.0), 38.0
WQ_FV_S = (4.0, 136.0)
WQ_FV_STEP = [(4, 4.5), (10, 3.8), (20, 2.2), (30, 0.8), (40, 0.4), (130, 0.9), (136, 1.5)]
WQ_DIKE_S0 = -345.0                    # past the set's edge (the breakwater's head is in France)
WQ_DIKE_JOG = -152.0
WQ_DIKE_INNER = 0.8                    # t of the lower quay's harbour edge
WQ_DIKE_WALK = (9.8, 16.4)             # the raised walk
WQ_DIKE_LOW, WQ_WALK_Z, WQ_OUT_LOW = 1.95, 5.75, 1.5
WQ_HF_STEP = [(-108, 10.0), (-104, 8.2), (-26.5, 8.2)]
WQ_PAVING, WQ_LOWQ, WQ_WALKC, WQ_ASPH = "#cdc8bd", "#77726e", "#93807a", "#5f5d5e"
WQ_COPING, WQ_FACE = "#e3dfd5", "#b4ad9f"


def _pw(pts):
    xs, ys = np.array(pts, float).T
    return lambda s: np.interp(s, xs, ys)


def _fpoly(F, s0, s1, t0, t1, step=2.0):
    """Polygon in frame F between t0(s) and t1(s) (numbers or functions)."""
    ss = np.unique(np.r_[np.arange(s0, s1, step), s1])
    f0 = t0 if callable(t0) else (lambda s, c=t0: np.full_like(s, c, dtype=float))
    f1 = t1 if callable(t1) else (lambda s, c=t1: np.full_like(s, c, dtype=float))
    a = [F.xy(s, t) for s, t in zip(ss, f0(ss))]
    b = [F.xy(s, t) for s, t in zip(ss[::-1], f1(ss[::-1]))]
    return orient(Polygon(a + b).buffer(0), 1.0)


def _frames():
    a = math.radians(HELI_AXIS)
    HF = Frame(None, HELI_O, (math.cos(a), math.sin(a)))
    b = math.radians(WQ_FV_AXIS)
    FF = Frame(None, WQ_FV_O, (math.cos(b), math.sin(b)))
    return HF, FF


def _sea_edge(F, ss, t_from, sea, t_max=30.0):
    """For each s, the t past t_from where the water starts (the land mask
    from the OSM coastline), median-smoothed."""
    seaR = Raster(sea.astype(np.float32))
    ts = np.arange(0.0, t_max, 0.25)
    out = np.full(len(ss), np.nan)
    for i, (s, tf) in enumerate(zip(ss, t_from)):
        q = F.O[None] + s * F.u[None] + (tf + ts)[:, None] * F.v[None]
        w = seaR.sample(q[:, 0], q[:, 1]) > 0.5
        k = np.nonzero(w)[0]
        if len(k):
            out[i] = tf + ts[k[0]]
    ok = np.isfinite(out)
    if ok.sum() < 3:
        return out
    out = np.interp(np.arange(len(ss)), np.nonzero(ok)[0], out[ok])
    return ndimage.median_filter(out, 5, mode="nearest")


def wq_flatten(mnt, mns):
    """Boats on the yard's hard standing and its travel lift, read as
    ground by the LiDAR: levelled to the quay (as flatten_port())."""
    out, n = mnt.copy(), 0
    for z in WQ_ZONES:
        x0, y0, x1, y1 = z.intersection(box(X0, Y0, X1, Y1)).bounds
        c0, c1 = int((x0 - X0) / RES), int((x1 - X0) / RES)
        r0, r1 = int((Y1 - y1) / RES), int((Y1 - y0) / RES)
        A = out[r0:r1, c0:c1]
        land = np.isfinite(A)
        F = np.where(land, A, 0.8)
        k = 4
        h, w = F.shape[0] // k * k, F.shape[1] // k * k
        Fs = F[:h, :w].reshape(h // k, k, w // k, k).min(axis=(1, 3))
        up = lambda a: np.pad(np.kron(a, np.ones((k, k))), ((0, F.shape[0] - h), (0, F.shape[1] - w)), mode="edge")
        quay = up(ndimage.percentile_filter(Fs, 20, size=11))
        around = up(ndimage.percentile_filter(Fs, 75, size=21))
        hump = land & (quay < 3.0) & (F - quay > 1.2) & (around < quay + 2.0)
        hump = ndimage.binary_dilation(hump, iterations=2) & land
        A[hump] = np.minimum(A[hump], quay[hump] + 0.1)
        n += int(hump.sum())
    mns = mns - np.nan_to_num(mnt - out, nan=0.0)
    # The boats on the hard standing are not built: no shadows or "trees"
    # from them either.
    yd = raster_mask(WQ_YARD)
    mns = np.where(yd & np.isfinite(out), out, mns)
    return out, mns, int(n * RES * RES)


def wq_setup(ways, mnt, sea):
    """Geometry of the two ports' quays: the breakwater's and the seawall's
    levels and faces, the generic quay bands, what to paint where."""
    global WQ
    if HELI is None:
        WQ = None
        return 0
    HF, FF = _frames()
    world = box(X0, Y0, X1, Y1)
    R = Raster(np.where(np.isfinite(mnt), mnt, np.nan).astype(np.float32))
    decks, steps, paint, rails = [], [], [], []

    # Cap d'Ail's outer breakwater.
    inner = _fpoly(HF, WQ_DIKE_S0, WQ_DIKE_JOG, WQ_DIKE_INNER, WQ_DIKE_WALK[0]).intersection(world)
    walk = orient(Polygon([HF.xy(*p) for p in [(WQ_DIKE_S0, WQ_DIKE_WALK[0]), (WQ_DIKE_JOG, WQ_DIKE_WALK[0]),
                                               (-148.0, 3.0), (-108.0, 3.0), (-108.0, 10.0), (-148.0, 10.0),
                                               (-154.0, WQ_DIKE_WALK[1]), (WQ_DIKE_S0, WQ_DIKE_WALK[1])]]), 1.0)
    walk = walk.intersection(world)
    hstep = _pw(WQ_HF_STEP)
    ss = np.arange(-153.0, -26.0, 1.0)
    tfrom = np.where(ss < -148, 10.0 + (-148 - ss) / 6 * 6.4, np.where(ss < -108, 10.0, hstep(ss)))
    tsea = _sea_edge(HF, ss, tfrom, sea)
    tsea = np.where(np.isfinite(tsea), np.clip(tsea, tfrom + 3.0, 17.0), 15.5)
    lsea = lambda s: np.interp(s, ss, tsea)
    low = _fpoly(HF, -153.2, -26.5, lambda s: np.interp(s, ss, tfrom), lsea, 1.0).difference(walk.buffer(0.02))
    up_s = np.arange(-108.0, -26.0, 2.0)
    upz = []
    for s in up_s:
        q = np.array([HF.xy(s, t) for t in np.arange(hstep(s) - 9.0, hstep(s) - 2.0, 1.0)])
        upz.append(np.nanmedian(R.sample(q[:, 0], q[:, 1])))
    upz = ndimage.median_filter(np.nan_to_num(np.array(upz), nan=WQ_WALK_Z), 5, mode="nearest")
    hz = lambda s: np.interp(s, up_s, upz)
    hup = _fpoly(HF, -108.0, -26.5, lambda s: hstep(s) - 8.0, hstep, 1.0)
    decks += [{"poly": inner, "z": lambda x, y: np.full(len(x), WQ_DIKE_LOW), "walls": "sea"},
              {"poly": walk, "z": lambda x, y: np.full(len(x), WQ_WALK_Z), "walls": "all"},
              {"poly": low, "z": lambda x, y: np.full(len(x), WQ_OUT_LOW), "walls": "sea"},
              {"poly": hup, "z": lambda x, y: hz((np.column_stack([x, y]) - HF.O) @ HF.u), "walls": None}]
    steps.append((HF, np.arange(-108.0, -26.4, 1.0), hstep, hz, WQ_OUT_LOW))
    # Paint: coping, road and parking under the wall; the walk's paving.
    paint += [(_fpoly(HF, WQ_DIKE_S0, WQ_DIKE_JOG, WQ_DIKE_INNER, WQ_DIKE_WALK[0]), WQ_COPING),
              (_fpoly(HF, WQ_DIKE_S0, WQ_DIKE_JOG, 1.6, 6.8), WQ_ASPH),
              (_fpoly(HF, WQ_DIKE_S0, WQ_DIKE_JOG, 6.8, WQ_DIKE_WALK[0] - 0.15), "#686566"),
              (walk, WQ_COPING), (walk.buffer(-0.6), WQ_WALKC), (low, WQ_LOWQ),
              (_fpoly(HF, -108.0, -26.5, lambda s: hstep(s) - 6.0, lambda s: hstep(s) - 0.5, 1.0), WQ_WALKC)]
    ticks = [(HF.xy(s, 6.8), HF.xy(s, WQ_DIKE_WALK[0] - 0.3)) for s in np.arange(WQ_DIKE_S0, WQ_DIKE_JOG - 4, 5.5)]
    lines = [([HF.xy(s, 6.8) for s in (WQ_DIKE_S0, WQ_DIKE_JOG)], 0.15)] + [(list(t), 0.12) for t in ticks]
    rails += [(walk, "all")]

    # Fontvieille's seawall.
    fstep = _pw(WQ_FV_STEP)
    fs = np.arange(WQ_FV_S[0], WQ_FV_S[1] + 0.1, 1.0)
    fsea = _sea_edge(FF, fs, fstep(fs) + 1.5, sea)
    fsea = np.where(np.isfinite(fsea), np.maximum(fsea, fstep(fs) + 3.0), fstep(fs) + 8.0)
    flow = _fpoly(FF, WQ_FV_S[0], WQ_FV_S[1], fstep, lambda s: np.interp(s, fs, fsea), 1.0)
    zl = []
    for s in fs[::4]:
        q = np.array([FF.xy(s, t) for t in np.arange(fstep(s) + 1.5, np.interp(s, fs, fsea) - 1.0, 0.75)])
        zl.append(np.nanmedian(R.sample(q[:, 0], q[:, 1])) if len(q) else np.nan)
    fz_low = float(np.nanmedian(zl)) if np.isfinite(zl).any() else 1.4
    fup_s = np.arange(WQ_FV_S[0], WQ_FV_S[1] + 0.1, 2.0)
    fupz = []
    for s in fup_s:
        q = np.array([FF.xy(s, t) for t in np.arange(fstep(s) - 9.0, fstep(s) - 2.0, 1.0)])
        fupz.append(np.nanmedian(R.sample(q[:, 0], q[:, 1])))
    fupz = ndimage.median_filter(np.nan_to_num(np.array(fupz), nan=5.7), 5, mode="nearest")
    fz = lambda s: np.interp(s, fup_s, fupz)
    fup = _fpoly(FF, WQ_FV_S[0], WQ_FV_S[1], lambda s: fstep(s) - 8.0, fstep, 1.0)
    decks += [{"poly": flow, "z": lambda x, y: np.full(len(x), fz_low), "walls": "sea"},
              {"poly": fup, "z": lambda x, y: fz((np.column_stack([x, y]) - FF.O) @ FF.u), "walls": None}]
    steps.append((FF, fs, fstep, fz, fz_low))
    paint += [(_fpoly(FF, WQ_FV_S[0], WQ_FV_S[1], lambda s: fstep(s) - 12.0, lambda s: fstep(s) - 0.5, 1.0), WQ_WALKC),
              (flow, WQ_LOWQ)]

    # The frames' own areas stay out of the generic quay bands, as do the
    # heliport (its apron, nets and the quay under them) and the rock armour.
    framed = unary_union([_fpoly(HF, WQ_DIKE_S0 - 10, 140.0, -6.0, 22.0),
                          _fpoly(FF, -10.0, WQ_FV_S[1] + 2.0, -16.0, 22.0)])
    # The armour runs from the line out to the water (on its right for
    # the mole, on its left for the bay and the jetty).
    rip = unary_union([LineString(l).buffer(3.0).union(LineString(l).buffer(-20.0 if k == 0 else 20.0, single_sided=True))
                       for k, l in enumerate(WQ_RIPRAP)])
    zone = unary_union(WQ_ZONES).intersection(world)
    land = ~sea
    dist = ndimage.distance_transform_edt(land) * RES
    zm = raster_mask(zone) & ~raster_mask(framed) & ~raster_mask(rip)
    # Quay level: the low part of the land just inland of the edge, spread.
    m_ = np.where(land & (dist > 1.5) & (dist < 6.0) & np.isfinite(mnt), mnt, np.nan)
    m_ = np.where(m_ < 4.0, m_, np.nan)
    idx = ndimage.distance_transform_edt(np.isnan(m_), return_distances=False, return_indices=True)
    qz = ndimage.median_filter(m_[idx[0], idx[1]], size=11)
    band = land & zm & (dist <= 8.0) & (qz < 3.5)
    # Roads keep their ground (their surfaces follow it).
    roadm = raster_mask(unary_union([LineString(p).buffer(5.0) for t, p, c in ways
                                     if t.get("highway") in ("residential", "service", "tertiary", "secondary", "primary",
                                                             "unclassified") and len(p) >= 2]).intersection(zone))
    lower = band & (dist <= 3.6) & ~roadm
    # Under the frames' decks the grid goes under water too.
    for F, s0, s1, t0, t1 in ((HF, WQ_DIKE_S0 - 10, WQ_DIKE_JOG, -4.0, 22.0),):
        lower |= raster_mask(_fpoly(F, s0, s1, t0, t1)) & np.isfinite(mnt)
    lower |= raster_mask(low.buffer(0.5)) | raster_mask(flow.buffer(0.5))
    lower |= raster_mask(_fpoly(HF, -108.0, -26.5, lambda s: hstep(s) - 4.0, hstep, 1.0))
    lower |= raster_mask(_fpoly(FF, WQ_FV_S[0], WQ_FV_S[1], lambda s: fstep(s) - 4.0, fstep, 1.0))
    pz = land & raster_mask(zone.difference(framed)) & (dist <= 30.0) & np.isfinite(mnt)
    pz &= (np.nan_to_num(mnt, nan=99) < 4.0)
    greens = [Polygon(p).buffer(0) for t, p, c in ways if c and len(p) > 3 and
              (t.get("leisure") in ("park", "garden", "pitch", "playground", "stadium") or t.get("landuse") in ("grass",))]
    if greens:
        pz &= ~raster_mask(unary_union(greens))
    deck_mask = raster_mask(unary_union([d["poly"] for d in decks]))
    WQ = {"HF": HF, "FF": FF, "decks": decks, "steps": steps, "paint": paint, "lines": lines, "rails": rails,
          "band": band, "qz": qz.astype(np.float32), "lower": lower, "pave": pz, "deck_mask": deck_mask,
          "zone": zone, "rip": rip,
          # Palms only on the quays themselves (not in the gardens behind).
          "palm_zone": (pz | band | deck_mask) & ~raster_mask(WQ_YARD.buffer(3.0))}
    print(f"  west quays: band {band.sum() * RES * RES:.0f} m2, lowered {lower.sum() * RES * RES:.0f} m2,"
          f" seawall lower quay {fz_low:.2f} m, upper {fupz.min():.2f}-{fupz.max():.2f} m")
    return int(band.sum() * RES * RES)


def wq_ground(mnt):
    """The grid under the decks and within 3.6 m of a quay wall goes under
    water: the decks and walls stand alone, with no slope in front."""
    if WQ is None:
        return mnt
    out = mnt.copy()
    out[WQ["lower"]] = -0.6
    return out


def wq_sea_paint(sea):
    """The sea mask for the ground map: the breakwater's walk stands where
    the coastline says water; it is painted as paving."""
    if WQ is None:
        return sea
    return sea & ~WQ["deck_mask"]


def _ground_deck(gm, pg, zf, lift=0.04):
    tr = triangulate(orient(pg, 1.0), area=8)
    if tr is None:
        return 0
    v, tri = tr
    z = np.asarray(zf(v[:, 0], v[:, 1]), float) + lift
    P = enu_fast(v[:, 0], v[:, 1], z)
    uv = np.column_stack([(v[:, 0] - X0) / (X1 - X0), (Y1 - v[:, 1]) / (Y1 - Y0)])
    gm.add(P, [0.0, 0.0, 1.0], [255, 255, 255], GROUND, uv=uv, idx=tri)
    return len(tri)


def _faces(m, pg, zf, seaR, mode, zground=1.0, wall=WQ_FACE):
    """Vertical faces on a deck's outline: into the water where the water
    is just outside (mode "sea"), down to the ground elsewhere too ("all").
    A pale coping on the edge over the water."""
    ring = np.asarray(orient(pg, 1.0).exterior.coords)[:, :2]
    a, b = ring[:-1], ring[1:]
    L = np.linalg.norm(b - a, axis=1)
    ok = L > 0.05
    a, b, L = a[ok], b[ok], L[ok]
    if not len(a):
        return 0.0
    u = (b - a) / L[:, None]
    out = np.column_stack([u[:, 1], -u[:, 0]])
    mid = (a + b) / 2
    # The world's edge: no face (the set just stops there).
    edge = (np.abs(mid[:, 0] - X0) < 0.3) | (np.abs(mid[:, 0] - X1) < 0.3) | \
        (np.abs(mid[:, 1] - Y0) < 0.3) | (np.abs(mid[:, 1] - Y1) < 0.3)
    wet = (seaR.sample(mid[:, 0] + out[:, 0] * 1.2, mid[:, 1] + out[:, 1] * 1.2) > 0.5) | \
        ~np.isfinite(seaR.sample(mid[:, 0] + out[:, 0] * 1.2, mid[:, 1] + out[:, 1] * 1.2))
    sel = ~edge & (wet if mode == "sea" else np.ones(len(a), bool))
    if not sel.any():
        return 0.0
    a, b, out, wet = a[sel], b[sel], out[sel], wet[sel]
    za, zb = np.asarray(zf(a[:, 0], a[:, 1]), float) + 0.04, np.asarray(zf(b[:, 0], b[:, 1]), float) + 0.04
    z0 = np.where(wet, -1.5, zground)
    col = np.array(hexrgb(wall), float)
    A0, B0 = enu_fast(a[:, 0], a[:, 1], z0), enu_fast(b[:, 0], b[:, 1], z0)
    A1, B1 = enu_fast(a[:, 0], a[:, 1], za), enu_fast(b[:, 0], b[:, 1], zb)
    w = np.column_stack([out, np.zeros(len(a))])
    _quad(m, A0, B0, B1, A1, col, w)
    # Coping: 0.5 m of pale stone, a hair proud, over the water.
    cw = wet
    if cw.any():
        ai, bi = a[cw] - out[cw] * 0.5, b[cw] - out[cw] * 0.5
        C0 = enu_fast(a[cw][:, 0], a[cw][:, 1], za[cw] + 0.03); C1 = enu_fast(b[cw][:, 0], b[cw][:, 1], zb[cw] + 0.03)
        D0 = enu_fast(ai[:, 0], ai[:, 1], za[cw] + 0.03); D1 = enu_fast(bi[:, 0], bi[:, 1], zb[cw] + 0.03)
        _quad(m, C0, C1, D1, D0, np.array(hexrgb(WQ_COPING), float), np.tile([[0.0, 0.0, 1.0]], (int(cw.sum()), 1)))
    return float(np.linalg.norm(b - a, axis=1).sum())


def wq_build(gm, m, sea, stats):
    """Decks (painted from the ground map), walls, steps and railings."""
    if WQ is None:
        return 0
    seaR = Raster(sea.astype(np.float32))
    n_tri, wall_m = 0, 0.0
    for d in WQ["decks"]:
        for pg in to_polys(d["poly"]):
            if pg.area < 2:
                continue
            n_tri += _ground_deck(gm, pg, d["z"])
            if d["walls"]:
                wall_m += _faces(m, pg, d["z"], seaR, d["walls"])
    # The steps between the two levels: a stone wall with a railing on top.
    for F, ss, tf, zf, zlow in WQ["steps"]:
        t = tf(ss)
        A = np.array([F.xy(s, tt) for s, tt in zip(ss, t)])
        zt = zf(ss) + 0.04
        k = len(A) - 1
        a, b = A[:-1], A[1:]
        out = np.tile(F.v, (k, 1))
        P0, P1 = enu_fast(a[:, 0], a[:, 1], np.full(k, zlow - 0.2)), enu_fast(b[:, 0], b[:, 1], np.full(k, zlow - 0.2))
        Q0, Q1 = enu_fast(a[:, 0], a[:, 1], zt[:-1]), enu_fast(b[:, 0], b[:, 1], zt[1:])
        _quad(m, P0, P1, Q1, Q0, np.array(hexrgb(WQ_FACE), float), np.column_stack([out, np.zeros(k)]))
        railing(m, a - out * 0.15, b - out * 0.15, zt[:-1], "#e8e8e2", 1.05, zt[1:])
        wall_m += float(np.linalg.norm(b - a, axis=1).sum())
    # Railings along both edges of the breakwater's raised walk.
    for pg_, _ in WQ["rails"]:
        for pg in to_polys(pg_):
            ring = np.asarray(orient(pg, 1.0).exterior.coords)[:, :2]
            a, b = ring[:-1], ring[1:]
            L = np.linalg.norm(b - a, axis=1)
            mid = (a + b) / 2
            edge = (np.abs(mid[:, 1] - Y0) < 0.3) | (np.abs(mid[:, 0] - X0) < 0.3)
            sel = (L > 4.0) & ~edge
            u = (b[sel] - a[sel]) / L[sel][:, None]
            inn = np.column_stack([-u[:, 1], u[:, 0]])
            railing(m, a[sel] + inn * 0.2, b[sel] + inn * 0.2, np.full(int(sel.sum()), WQ_WALK_Z + 0.04), "#e8e8e2", 1.05)
    # Generic quays: a level band by the water, its wall and coping.
    band = WQ["band"]
    qR = Raster(WQ["qz"])
    lab, nl = ndimage.label(band)
    zq = lambda x, y: np.nan_to_num(qR.sample(x, y), nan=1.5)
    for j, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        sub = lab[sl] == j
        if sub.sum() < 40:
            continue
        poly = cells_to_poly(sub, X0 + sl[1].start * RES, Y1 - sl[0].start * RES, RES)
        for pg in to_polys(poly):
            if pg.area < 8:
                continue
            pg = orient(pg.simplify(0.3), 1.0)
            n_tri += _ground_deck(gm, pg, zq, 0.05)
            wall_m += _faces(m, pg, zq, seaR, "sea")
    stats["west_quay_wall_m"] = int(wall_m)
    return int(wall_m)


def wq_paint(img, xy, poly, line):
    """Clean paving on the quays (no photo lawn), the yard's hard standing,
    the breakwater's road and walk, the seawall's promenade."""
    if WQ is None:
        return
    S = img.size[0]
    pv = Image.fromarray((WQ["pave"] * 255).astype(np.uint8)).resize((S, S), Image.NEAREST)
    img.paste(tuple(hexrgb(WQ_PAVING)), (0, 0, S, S), pv)
    poly(np.asarray(WQ_YARD.exterior.coords), "#8a8884")
    for g, col in WQ["paint"]:
        for q in to_polys(g):
            poly(np.asarray(q.exterior.coords), col)
    for pts_, w in WQ["lines"]:
        line(np.asarray(pts_), "#eeeeea", w)


def wq_palms(m, mns, mnt, bmask, placed, rnd, stats):
    """Tall palms on the quays: 7-16 m in the LiDAR, a crown of a few m²,
    but grey in the photo (fronds over paving), so trees() left them out."""
    if WQ is None:
        return 0
    chm = np.nan_to_num(mns - mnt, nan=0)
    zone = WQ["palm_zone"] & ~(bmask > 0)
    hi = (chm > 3.0) & zone
    lab, n = ndimage.label(hi)
    if not n:
        return 0
    sizes = ndimage.sum(hi, lab, np.arange(1, n + 1))
    tall = ndimage.maximum(chm, lab, np.arange(1, n + 1))
    keep = np.isin(lab, np.nonzero((sizes >= 8) & (sizes <= 400) & (tall >= 7.0))[0] + 1)
    sm = ndimage.gaussian_filter(np.where(keep, chm, 0), 1.0)
    pk = np.argwhere((sm == ndimage.maximum_filter(sm, size=9)) & keep & (sm > 5.0))
    pl = list(placed)
    k = 0
    for r, c in pk:
        x, y = cell_xy(r, c)
        if pl and min(math.hypot(x - px, y - py) for px, py in pl) < 4.0:
            continue
        h = float(np.clip(chm[max(0, r - 2):r + 3, max(0, c - 2):c + 3].max(), 6.0, 16.0))
        z = mnt[r, c] if np.isfinite(mnt[r, c]) else 1.5
        add_tree(m, x, y, z, h, float(rnd.uniform(2.6, 3.3)), "palm", rnd)
        pl.append((x, y))
        k += 1
    stats["trees"] += k
    stats["quay_palms"] = k
    return k


# ---------------------------------------------------------------- parks ----
# Parks and gardens all over the set: until now each was a flat OSM green
# with the 50 cm photo's green and the LiDAR canopy blurred over it (soft
# dark blots where the trees' shade fell), and Fontvieille's big park, an
# OSM multipolygon relation, was not even read. Now every mapped park and
# garden is sorted, from the 20 cm photo, the LiDAR heights and OSM, into
# lawns, paths, shrub beds and flower beds, each laid as its own slab with
# crisp edges and drawn by the viewer in world metres (blades and mown
# stripes on the lawns, grit on the paths, leafy mounds in the beds,
# flowers in the beds OSM maps), so they stay sharp up close. Each slab's
# vertex colour is the photo's own colour there (its chroma only, so the
# photo's tree shadows do not come back as blots); the ground map under
# the slabs is a plain grey that carries just the baked shade.
PARK_LAWN, PARK_PATH, PARK_BED, PARK_FLOWERS, PARK_PAVED = 14, 15, 7, 6, 5   # GROUND styles
PARK_LAWN_C, PARK_BED_C = "#8db455", "#5f8a3c"   # lawn and leaf greens (sRGB)
PARK_GREY = "#9a9a9a"                  # ground map under the park slabs (shade only)
PARK_RES = 0.25                        # m, sorting grid
PARK_KINDS = ("park", "garden")
PARK_SKIP = ("Esplanade Rainier III", "Square Théodore Gastaud", "Jardin de l'Unesco")
PARKS = None                           # {"slabs": union of the slabs, "names": [...]}


def park_relations():
    """leisure=park/garden multipolygon relations (Fontvieille's park) as
    (tags, polygon)."""
    root = ET.parse(RAW / "osm.xml").getroot()
    rels = []
    for rel in root.findall("relation"):
        t = {k.get("k"): k.get("v") for k in rel.findall("tag")}
        if t.get("type") == "multipolygon" and t.get("leisure") in PARK_KINDS:
            rels.append((t, [(m.get("ref"), m.get("role")) for m in rel.findall("member") if m.get("type") == "way"]))
    if not rels:
        return []
    need = {r for _, ms in rels for r, _ in ms}
    wnodes = {w.get("id"): [nd.get("ref") for nd in w.findall("nd")] for w in root.findall("way") if w.get("id") in need}
    nids = {n for v in wnodes.values() for n in v}
    ll = {n.get("id"): (float(n.get("lon")), float(n.get("lat"))) for n in root.findall("node") if n.get("id") in nids}
    tl = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    out = []
    for t, ms in rels:
        rings = {"outer": [], "inner": []}
        for ref, role in ms:
            p_ = [ll[n] for n in wnodes.get(ref, []) if n in ll]
            if len(p_) >= 2:
                x, y = tl.transform(*np.array(p_).T)
                rings["inner" if role == "inner" else "outer"].append(LineString(np.column_stack([x, y])))
        outer = unary_union(list(shapely.ops.polygonize(unary_union(rings["outer"])))) if rings["outer"] else Polygon()
        inner = unary_union(list(shapely.ops.polygonize(unary_union(rings["inner"])))) if rings["inner"] else Polygon()
        g = outer.difference(inner).buffer(0)
        if not g.is_empty:
            out.append((t, g))
    return out


def _runs_to_poly(mask, x0, y1, res):
    """Raster cells (mask True) as polygons, from row runs (fast)."""
    boxes = []
    for r in range(mask.shape[0]):
        row = mask[r]
        if not row.any():
            continue
        d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
        st, en = np.nonzero(d == 1)[0], np.nonzero(d == -1)[0]
        yt, yb = y1 - r * res, y1 - (r + 1) * res
        boxes += [box(x0 + a * res, yb, x0 + b * res, yt) for a, b in zip(st, en)]
    if not boxes:
        return Polygon()
    return unary_union(boxes)


def _taken_mask(gm, x0, y1, nx, ny, res):
    """Where other passes already laid ground slabs (any GROUND style but
    the plain map's 8), on the park grid: rasterised from their map uv."""
    img = Image.new("L", (nx, ny), 0)
    d = ImageDraw.Draw(img)
    off = 0
    for P, C, UV, I in zip(gm.p, gm.c, gm.uv, gm.i):
        n = len(P)
        a = int(C[0, 3]) if n else 8
        if n and a // 16 == 0 and a % 16 != 8 and len(I):
            c_ = (X0 + UV[:, 0] * (X1 - X0) - x0) / res
            r_ = (y1 - (Y1 - UV[:, 1] * (Y1 - Y0))) / res
            if not (c_.max() < 0 or r_.max() < 0 or c_.min() > nx or r_.min() > ny):
                for k in (I - off).reshape(-1, 3):
                    d.polygon([(c_[j], r_[j]) for j in k], fill=255)
        off += n
    return np.asarray(img) > 0


def parks_setup(ways, fps):
    """The parks and gardens to rebuild, as (name, polygon)."""
    cands = []
    for t, p_, c in ways:
        if c and len(p_) > 3 and t.get("leisure") in PARK_KINDS:
            cands.append((t, Polygon(p_).buffer(0)))
    cands += park_relations()
    blds = unary_union(fps) if fps else Polygon()
    out = []
    for t, g in cands:
        nm = t.get("name", "")
        if nm in PARK_SKIP or g.area < 60:
            continue
        g = g.difference(PECHE_DECK.buffer(1.0))          # the Pêcheurs garden has its own layout
        if g.area < 60:
            continue
        if FM is not None:
            g = g.difference(FM["garden"].buffer(0.5))    # the Fairmont's roof garden has its own
            if g.area < 60:
                continue
        if g.intersection(blds).area > 0.5 * g.area:
            continue                                     # other roof gardens
        out.append((nm, g))
    return out


def _park_colour(o, sel, sig_px, fallback, sat=1.25, lum_to=None):
    """Per-cell colour field from the photo over the cells `sel`: the
    photo's chroma, blurred over that class only (normalised), at a fixed
    brightness, so shade and dark trees do not print through."""
    o = o.astype(np.float32)
    lum = o.mean(-1, keepdims=True)
    chroma = o / np.maximum(lum, 8.0)
    w = sel.astype(np.float32)
    ws = ndimage.gaussian_filter(w, sig_px) + 1e-4
    ch = np.stack([ndimage.gaussian_filter(chroma[..., k] * w, sig_px) / ws for k in range(3)], -1)
    if sel.sum() > 20:
        med = np.median(chroma[sel], 0)
        lm = float(np.median(lum[sel])) if lum_to is None else lum_to
    else:
        f = np.array(fallback, np.float32)
        med, lm = f / f.mean(), f.mean()
    ch = np.where((ws > 0.05)[..., None], ch, med[None, None])
    ch = 1.0 + (ch - 1.0) * sat
    return np.clip(ch * lm, 0, 255), med, lm


def parks(gm, bm, ways, pts, mnt, mns, ortho_hi, gz, fps, stats):
    """Lay every park's lawns, paths and beds (see the note above)."""
    global PARKS
    PARKS = {"slabs": Polygon(), "names": []}
    if ortho_hi is None:
        return 0
    found = parks_setup(ways, fps)
    if not found:
        return 0
    mntR = Raster(mnt)
    k_ = ortho_hi.size[0] / (X1 - X0)                        # photo px per metre
    blds = unary_union([p.buffer(0.3, join_style=2) for p in fps])
    # What is not park ground: water, pools, fountains, roads, the track.
    wet = [Polygon(p_).buffer(0) for t, p_, c in ways if c and len(p_) > 3 and (
        t.get("natural") == "water" or t.get("leisure") == "swimming_pool" or t.get("amenity") == "fountain"
        or t.get("water"))]
    roads = []
    for t, p_, c in ways:
        hw = t.get("highway")
        if hw in ROAD_W and hw != "service" and not c and t.get("tunnel") is None and not t.get("bridge"):
            roads.append(LineString(p_).buffer(ROAD_W[hw] / 2 + 1.0))
    if TRACK is not None:
        roads.append(TRACK["line"].buffer(TRACK_HALF + 3.0))
    if SW is not None:
        roads.append(SW["poly"])
    hard = unary_union(wet + roads + [blds])
    # Paths from OSM, by width and surface.
    PW = {"path": 1.4, "footway": 2.2, "cycleway": 2.5, "pedestrian": 4.0, "steps": 2.4, "track": 3.0,
          "service": 3.5, "bridleway": 2.5}
    PAVED = ("paving_stones", "concrete", "concrete:plates", "concrete:tiles", "sett", "paved", "stone",
             "unhewn_cobblestone", "cobblestone", "tiles", "marble")
    LOOSE = ("gravel", "fine_gravel", "compacted", "ground", "dirt", "unpaved", "sand", "pebblestone", "earth")
    lines = []
    for t, p_, c in ways:
        hw = t.get("highway")
        if hw in PW and t.get("tunnel") is None and t.get("layer", "0") not in ("-1", "-2") and not t.get("bridge"):
            try:
                w = float(t.get("width", "").split()[0])
            except (ValueError, IndexError):
                w = PW[hw]
            w = float(np.clip(w, 1.0, 6.0))
            s = t.get("surface", "")
            sk = "paved" if s in PAVED or hw == "steps" else ("asphalt" if s == "asphalt" else ("loose" if s in LOOSE else ""))
            g = Polygon(p_).buffer(0) if (c and t.get("area") == "yes") else LineString(p_).buffer(w / 2, cap_style=1)
            lines.append((g, sk))
    flowerbeds = [Polygon(p_).buffer(0) for t, p_, c in ways if c and len(p_) > 3 and t.get("landuse") == "flowerbed"]
    gy_, gx_ = np.gradient(np.nan_to_num(mnt, nan=0.0), RES)
    slope = np.hypot(gx_, gy_)
    chm = np.nan_to_num(mns - mnt, nan=0.0)
    R = PARK_RES
    cnt = {k: 0.0 for k in ("lawn", "path", "bed", "flowers", "paved")}
    slab_all = []
    done = Polygon()
    for nm, g0 in sorted(found, key=lambda t: -t[1].area):
        g = g0.difference(done)
        done = done.union(g0)
        g = g.difference(hard)
        if g.is_empty or g.area < 40:
            continue
        bx0, by0, bx1, by1 = g.bounds
        bx0, by0 = math.floor(bx0) - 1, math.floor(by0) - 1
        bx1, by1 = math.ceil(bx1) + 1, math.ceil(by1) + 1
        nx, ny = int((bx1 - bx0) / R), int((by1 - by0) / R)
        cx = bx0 + (np.arange(nx) + 0.5) * R
        cy = by1 - (np.arange(ny) + 0.5) * R
        CX, CY = np.meshgrid(cx, cy)
        inside = contains_xy(g, CX, CY)
        if inside.sum() < 200:
            continue
        # Photo, LiDAR on the grid.
        pa0, pb0 = int((bx0 - X0) * k_), int((Y1 - by1) * k_)
        pa1, pb1 = int(math.ceil((bx1 - X0) * k_)) + 1, int(math.ceil((Y1 - by0) * k_)) + 1
        ph = np.asarray(ortho_hi.crop((pa0, pb0, pa1, pb1))).astype(np.float32)
        fi = (CX - X0) * k_ - pa0 - 0.5
        fj = (Y1 - CY) * k_ - pb0 - 0.5
        o = np.stack([ndimage.map_coordinates(ph[..., k], [fj, fi], order=1, mode="nearest") for k in range(3)], -1)
        ri = np.clip(((Y1 - CY) / RES).astype(int), 0, mnt.shape[0] - 1)
        ci = np.clip(((CX - X0) / RES).astype(int), 0, mnt.shape[1] - 1)
        h = chm[ri, ci]
        sl = slope[ri, ci]
        exg = 2 * o[..., 1] - o[..., 0] - o[..., 2]
        lum = o.mean(-1)
        veg = (exg > 6) & (o[..., 1] >= o[..., 0])
        dark = lum < 70                                  # deep shade or a tree crown: unknown
        pale = (lum > 135) & (exg < 8) & (h < 0.6) & ~veg
        taken = _taken_mask(gm, bx0, by1, nx, ny, R)
        # OSM paths and flower beds, kept as vectors (smooth edges).
        pk = {1: [], 2: [], 3: [], 0: []}                # 1 paved, 2 asphalt, 3 loose, 0 unknown
        for lg, sk in lines:
            if lg.intersects(g):
                pk[{"paved": 1, "asphalt": 2, "loose": 3}.get(sk, 0)].append(lg.intersection(g))
        pk = {k: unary_union(v) if v else Polygon() for k, v in pk.items()}
        osm_paths = unary_union(list(pk.values()))
        fbv = unary_union([q.intersection(g) for q in flowerbeds if q.intersects(g)] or [Polygon()])
        # Classes from the photo and LiDAR: 1 lawn, 2 path, 3 bed.
        cl = np.ones(inside.shape, np.int8)
        cl[pale] = 2
        cl[veg & (h > 0.35) & (h < 3.0)] = 3                # shrubs and hedges
        unknown = (h >= 3.0) | dark                      # under the trees
        # Unknown cells take the class of the nearest known one (a lawn
        # runs on under a tree; a bed under a hedge of trees).
        known = inside & ~unknown
        if known.any():
            _, (ir, ic) = ndimage.distance_transform_edt(~known, return_indices=True)
            cl = np.where(unknown, cl[ir, ic], cl)
        # A wood (the tree cover nearly closed over a big patch) is a bed of
        # undergrowth, not lawn.
        cover = ndimage.uniform_filter((h >= 3.0).astype(np.float32), size=int(6 / R))
        cl[(cover > 0.85) & (cl == 1)] = 3
        # Smooth the classes (soft vote over ~0.5 m), so their edges are
        # curves, not the grid's steps.
        vote = np.stack([ndimage.gaussian_filter((cl == c_).astype(np.float32), 2.0) for c_ in (1, 2, 3)])
        cl = (np.argmax(vote, 0) + 1).astype(np.int8)
        none = (sl > 0.8) | taken                        # cliffs and rockeries keep the rock shader
        none = ndimage.binary_opening(none, iterations=2)

        # (Overlays on a 1 cm grid: the raster outlines are full of nearly
        # coincident edges that make plain GEOS overlays fail.)
        G_ = 0.01
        def A(x):                                         # polygons only (overlays drop slivers to lines)
            ps = [q for q in shapely.get_parts(x) if q.geom_type == "Polygon" and not q.is_empty] if not x.is_empty else []
            for q in list(shapely.get_parts(x)) if not x.is_empty else []:
                if q.geom_type in ("MultiPolygon", "GeometryCollection"):
                    ps += to_polys(q)
            return MultiPolygon(ps) if ps else Polygon()
        D = lambda x, y: A(shapely.difference(A(x), A(y), grid_size=G_))
        I = lambda x, y: A(shapely.intersection(A(x), A(y), grid_size=G_))
        U = lambda x, y: A(shapely.union(A(x), A(y), grid_size=G_))
        P_ = lambda x: A(shapely.set_precision(A(x).buffer(0), G_))
        gg = P_(g)
        osm_p = P_(osm_paths)
        fbv = P_(fbv)

        def vec(mk):
            if mk.sum() * R * R < 1.0:
                return Polygon()
            q = _runs_to_poly(mk, bx0, by1, R).simplify(0.3)
            q = q.buffer(0.35).buffer(-0.7).buffer(0.35)
            return I(P_(q), gg)
        none_v = D(vec(none), osm_p)
        avail = D(gg, none_v)
        pieces = {}
        pieces["osm_paths"] = I(osm_p, avail)
        pieces["flowers"] = D(I(fbv, avail), osm_p)
        taken_v = U(pieces["osm_paths"], pieces["flowers"])
        pieces["pale"] = D(I(vec(cl == 2), avail), taken_v)
        taken_v = U(taken_v, pieces["pale"])
        pieces["bed"] = D(I(vec(cl == 3), avail), taken_v)
        taken_v = U(taken_v, pieces["bed"])
        pieces["lawn"] = D(avail, taken_v)
        for k in pk:
            pk[k] = P_(pk[k])
        # Colours: the photo's greens are dull and hazy: it gives only the
        # variation (drier, bluer patches) around a set lawn and leaf green.
        def _around(f, ref, k=0.7):
            r_ = np.array(hexrgb(ref), np.float32)
            v_ = f / np.maximum(np.median(f.reshape(-1, 3), 0), 1.0)[None, None]
            return np.clip(r_[None, None] * np.clip(v_, 0.75, 1.3) ** k, 0, 255)
        lawn_c, _, _ = _park_colour(o, (cl == 1) & veg & ~dark, 1.5 / R, hexrgb(PARK_LAWN_C), sat=1.2)
        lawn_c = _around(lawn_c, PARK_LAWN_C)
        bed_c, _, _ = _park_colour(o, (cl == 3) & veg & ~dark, 1.5 / R, hexrgb(PARK_BED_C), sat=1.2)
        bed_c = _around(bed_c, PARK_BED_C)
        path_c, _, _ = _park_colour(o, pale, 2.0 / R, hexrgb("#d8cdb8"), sat=0.8, lum_to=196.0)
        path_c = np.clip(path_c, 120, 238)
        flat = lambda hx: np.broadcast_to(np.array(hexrgb(hx), np.float32), path_c.shape)
        jobs = [(pieces["lawn"], PARK_LAWN, lawn_c, "lawn"), (pieces["bed"], PARK_BED, bed_c, "bed"),
                (pieces["flowers"], PARK_FLOWERS, bed_c, "flowers"), (pieces["pale"], PARK_PATH, path_c, "path"),
                (I(pk[1], pieces["osm_paths"]), PARK_PAVED, path_c, "paved"),
                (D(I(pk[2], pieces["osm_paths"]), pk[1]), PARK_PATH, flat("#8d8c88"), "path"),
                (D(D(I(pk[3], pieces["osm_paths"]), pk[1]), pk[2]), PARK_PATH,
                 path_c * 0.4 + flat("#d6c49e") * 0.6, "path"),
                (D(D(D(I(pk[0], pieces["osm_paths"]), pk[1]), pk[2]), pk[3]), PARK_PATH, path_c, "path")]
        for poly, sty, cf, key in jobs:
            if poly.is_empty:
                continue
            a_ = _park_slab(gm, poly, mntR, gz, sty, cf, bx0, by1, R)
            cnt[key] += a_
            if a_ > 0:
                slab_all.append(poly)
        PARKS["names"].append(nm or "(unnamed)")
    PARKS["slabs"] = unary_union(slab_all) if slab_all else Polygon()
    for k, v in cnt.items():
        stats[f"park_{k}_m2"] = round(v)
    stats["parks"] = len(PARKS["names"])
    print("parks:", ", ".join(n for n in PARKS["names"] if n != "(unnamed)"))
    return len(PARKS["names"])


def _safe_pieces(poly, depth=0):
    """Polygons Triangle can take: valid, no vertex used twice (rings that
    touch at a corner); shrunk a little more each try until they are."""
    out = []
    for q in to_polys(poly):
        q = shapely.set_precision(q, 0.01)
        if q.is_empty or q.geom_type != "Polygon" or q.area < 0.6:
            continue
        vv = np.concatenate([np.asarray(r.coords)[:-1] for r in [q.exterior, *q.interiors]])
        ok = q.is_valid and len(np.unique(np.round(vv * 100).astype(np.int64), axis=0)) == len(vv)
        if ok:
            out.append(q)
        elif depth < 3:
            out += _safe_pieces(q.buffer(-0.04 * (depth + 1), join_style=2).buffer(0), depth + 1)
    return out


def _park_slab(gm, poly, mntR, gz, style, cf, bx0, by1, R, lift=0.08, area=2.5):
    """A ground slab like _slab, with a colour per vertex from the field cf."""
    n_ = 0.0
    # (Raster outlines touch themselves at corners; Triangle crashes on
    # that, so the pieces are pulled apart a little first.)
    poly = poly.buffer(-0.03, join_style=2).buffer(0.03, join_style=2).simplify(0.06)
    for q in _safe_pieces(poly):
        tr = triangulate(orient(q, 1.0), area=area)
        if tr is None:
            continue
        v, tri = tr
        z = mntR.sample(v[:, 0], v[:, 1])
        if gz is not None:
            fi, fj = (v[:, 0] - X0) / GRID, (Y1 - v[:, 1]) / GRID
            z = np.fmax(z, ndimage.map_coordinates(gz, [fj, fi], order=1, mode="nearest"))
        if not np.isfinite(z).any():
            continue
        z = np.where(np.isfinite(z), z, np.nanmedian(z))
        zt = z[tri]
        tri = tri[(zt.max(1) - zt.min(1)) < 2.5]
        if not len(tri):
            continue
        ci = np.clip(((v[:, 0] - bx0) / R).astype(int), 0, cf.shape[1] - 1)
        ri = np.clip(((by1 - v[:, 1]) / R).astype(int), 0, cf.shape[0] - 1)
        col = cf[ri, ci]
        P = enu_fast(v[:, 0], v[:, 1], z + lift)
        uv = np.column_stack([(v[:, 0] - X0) / (X1 - X0), (Y1 - v[:, 1]) / (Y1 - Y0)])
        gm.add(P, [0.0, 0.0, 1.0], col, GROUND, uv=uv, idx=tri, style=style)
        n_ += q.area
    return n_


# ------------------------------------------------------- Monaco Heliport ----
# The Héliport de Monaco (LNMC) on the sea front of Fontvieille: a concrete
# apron 170 m long on the sea wall, six stands in a row (1 to 6, the second
# one the white "H" of the landing area) and two square platforms, A and B,
# out over the water at either end. Everything is placed from open data:
# OpenStreetMap (the heliport, the eight helipads, the terminal, the hangar,
# the control tower) and measured on the IGN 20 cm photo (each pad's rings
# fitted to the paint: 7.45 m and 2.9 m, the arms at 55 degrees, the blue
# taxi lines, the yellow and black hatching, the red and white edge) and in
# the LiDAR (the apron at 5.8 m NGF sloping 30 cm to the sea, A and B at
# 6.4 m, a quay at 1.4 m under the safety nets along the edge).
# Frame: s along the apron from pad 1's centre (south-west to north-east),
# t out to sea.
HELI_O = (1055977.8, 6301799.3)
HELI_AXIS = 48.4                       # degrees, east to north
HELI_EDGE = 8.2                        # t of the apron's sea edge
HELI_BACK = -16.0                      # t, behind the terminal and hangar fronts
HELI_ENDS = (-30.0, 137.0)             # s, the hatched ends
HELI_JUTS = (-17.6, 125.3)             # s of pads A and B, the platforms' centre lines
HELI_JUT_HALF = 8.85
HELI_JUT_END = 26.6                    # t of the platforms' sea side
HELI_RAMP = 2.8                        # m from the apron's edge up to a platform
HELI_NET = 4.4                         # m, the safety net frame out from the edge
HELI_QUAY = 1.4                        # m NGF, the quay under the nets (LiDAR)
# Pads: s, t, outer ring radius, inner ring radius, colour, arms. An arm is
# (side along s, side across t, arrow): arms run at 55 degrees to the apron,
# from the inner ring to the outer one; an arrow carries on past the ring to
# a head pointing at the taxi line. "out": a short arrow from the ring only.
HELI_ARM_DEG = 55.0
_ALL4 = ((-1, -1, True), (1, -1, True), (-1, 1, False), (1, 1, False))
HELI_PADS = [
    ("1", 0.0, -0.30, 7.45, 2.9, "yellow", ((-1, -1, True), (1, 1, False))),
    ("2", 20.2, -0.16, 7.45, 2.9, "white", _ALL4),
    ("3", 40.41, -0.16, 7.45, 2.9, "yellow", _ALL4),
    ("4", 60.7, -0.06, 7.45, 2.9, "yellow", _ALL4),
    ("5", 84.69, -0.09, 7.45, 2.9, "yellow", _ALL4),
    ("6", 108.95, 0.04, 7.45, 2.9, "yellow", ((1, -1, True), (-1, 1, False))),
    ("A", -17.61, 19.78, 6.1, 3.0, "yellow", ((-1, -1, "out"), (1, -1, "out"))),
    ("B", 125.34, 20.04, 6.1, 3.0, "yellow", ((-1, -1, "out"), (1, -1, "out"))),
]
HELI_TAXI_T = -10.75                   # the blue line along the back of the stands
HELI_TAXI_S = (-8.0, 117.0)            # where it turns down to A and B
HELI_PAINT = {"yellow": "#e3bd3c", "white": "#f1f0ea", "blue": "#7fb2d8", "concrete": "#9c9d9b",
              "face": "#c4c0b6", "steel": "#d9dcdc", "net": "#5d6266"}
HELI_HANGAR = "BATIMENT0000000074078998"
HELI_TERMINAL = "BATIMENT0000000074079010"
# Light buildings on the apron's ends: plain white, no windows.
HELI_PLAIN = {HELI_HANGAR: "#e3e3df", "BATIMENT0000000074079066": "#f2f1ec",
              "BATIMENT0000002009026616": "#f2f1ec"}
HELI_TOWER = (28.1, -15.0)             # OSM aeroway=tower, on the terminal's corner
HELI_TOWER_TOP = 15.1                  # m NGF (LiDAR)
HELI_PARKED = [("5", 90.0)]            # a red helicopter on stand 5 (the photo), nose: degrees left of +s
HELI = None


def heliport_setup(ways, mnt, feats):
    """The apron's outline (OpenStreetMap's heliport, cut at the sea edge,
    less the buildings on it, plus the two platforms) and its levels."""
    global HELI
    F = Frame(Mesh(), HELI_O, (math.cos(math.radians(HELI_AXIS)), math.sin(math.radians(HELI_AXIS))))
    osm = None
    for t_, p_, closed in ways:
        if closed and t_.get("aeroway") == "heliport" and len(p_) >= 4:
            osm = Polygon(p_).buffer(0.5)
    if osm is None or not box(X0, Y0, X1, Y1).contains(osm.centroid):
        HELI = None
        return 0
    main = F.poly(HELI_ENDS[0], HELI_ENDS[1], HELI_BACK, HELI_EDGE).intersection(osm)
    juts = []
    for sc in HELI_JUTS:
        a, b, c = HELI_JUT_HALF, HELI_JUT_END, 2.8
        juts.append(orient(Polygon([F.xy(sc - a, HELI_EDGE - 0.01), F.xy(sc + a, HELI_EDGE - 0.01), F.xy(sc + a, b - c),
                                    F.xy(sc + a - c, b), F.xy(sc - a + c, b), F.xy(sc - a, b - c)]), 1.0))
    bl = []
    for f in feats:
        g = shapely.force_2d(shape(f["geometry"])).buffer(0)
        if g.intersects(main):
            bl.append(g.buffer(0.1, join_style=2))
    deck = unary_union([main] + juts).difference(unary_union(bl)) if bl else unary_union([main] + juts)
    deck = max(to_polys(deck), key=lambda q: q.area)
    deck = orient(Polygon(deck.exterior.simplify(0.05)), 1.0)
    # Levels: a plane through the apron's terrain, the platforms' own level.
    R = Raster(mnt)
    ss, tt = np.meshgrid(np.arange(-25.0, 131.0, 2.0), np.arange(-11.0, 6.5, 1.5))
    P = F.O[None] + ss.ravel()[:, None] * F.u[None] + tt.ravel()[:, None] * F.v[None]
    z = R.sample(P[:, 0], P[:, 1])
    ok = np.isfinite(z)
    A = np.column_stack([np.ones(ok.sum()), ss.ravel()[ok], tt.ravel()[ok]])
    coef = np.linalg.lstsq(A, z[ok], rcond=None)[0]
    for _ in range(2):                             # drop the odd parked van and re-fit
        r_ = z[ok] - A @ coef
        keep = np.abs(r_) < 0.15
        coef = np.linalg.lstsq(A[keep], z[ok][keep], rcond=None)[0]
    jz = []
    for sc in HELI_JUTS:
        sj, tj = np.meshgrid(np.linspace(sc - 5, sc + 5, 6), np.linspace(14, 24, 6))
        Q = F.O[None] + sj.ravel()[:, None] * F.u[None] + tj.ravel()[:, None] * F.v[None]
        jz.append(float(np.nanmedian(R.sample(Q[:, 0], Q[:, 1]))))
    HELI = {"F": F, "deck": deck, "main": deck.difference(unary_union(juts).buffer(-0.02)), "juts": juts,
            "coef": coef, "jut_z": jz, "osm": osm}
    print(f"  heliport: apron {deck.area:.0f} m2, plane {coef[0]:.2f} {coef[1]:+.4f}/m s {coef[2]:+.4f}/m t,"
          f" platforms at {jz[0]:.2f} and {jz[1]:.2f} m")
    return deck.area


def heli_st(x, y):
    F = HELI["F"]
    d = np.column_stack([np.asarray(x, float) - F.O[0], np.asarray(y, float) - F.O[1]])
    return d @ F.u, d @ F.v


def heli_z(s, t):
    """The apron's level at (s, t): the plane, then up the short ramps
    onto the platforms."""
    s, t = np.broadcast_arrays(np.asarray(s, float), np.asarray(t, float))
    c = HELI["coef"]
    z = c[0] + c[1] * s + c[2] * np.minimum(t, HELI_EDGE)
    for sc, jz in zip(HELI_JUTS, HELI["jut_z"]):
        on = (np.abs(s - sc) <= HELI_JUT_HALF + 0.05) & (t > HELI_EDGE)
        w = np.clip((t - HELI_EDGE) / HELI_RAMP, 0.0, 1.0)
        z = np.where(on, z * (1 - w) + jz * w, z)
    return z


def heliport_ground(mnt):
    """Terrain under the apron sunk out of sight, and the quay below the
    sea edge level, so the 3 m ground grid does not run the drop out as a
    slope."""
    if HELI is None:
        return mnt
    out = mnt.copy()
    zone = HELI["deck"].buffer(GRID + 2.0, join_style=2)
    x0, y0, x1, y1 = zone.bounds
    c0, c1 = max(int((x0 - X0) / RES), 0), min(int((x1 - X0) / RES) + 1, mnt.shape[1])
    r0, r1 = max(int((Y1 - y1) / RES), 0), min(int((Y1 - y0) / RES) + 1, mnt.shape[0])
    rr, cc = np.mgrid[r0:r1, c0:c1]
    cx, cy = cell_xy(rr.ravel(), cc.ravel())
    s, t = heli_st(cx, cy)
    sub = out[r0:r1, c0:c1].ravel()
    inside = contains_xy(HELI["deck"], cx, cy)
    near_edge = (t > HELI_EDGE - GRID - 1.6) & (t < HELI_EDGE + HELI_NET + 3.0) & \
        (s > HELI_JUTS[0] - HELI_JUT_HALF) & (s < HELI_JUTS[1] + HELI_JUT_HALF) & ((t >= HELI_EDGE) | inside)
    on_jut = contains_xy(unary_union(HELI["juts"]).buffer(GRID + 1.0, join_style=2), cx, cy) & (t > HELI_EDGE - GRID - 1.6)
    low = (near_edge | on_jut) & np.isfinite(sub)
    sub[low] = np.minimum(sub[low], HELI_QUAY - 0.05)
    deep = inside & ~low & np.isfinite(sub)
    sub[deep] = sub[deep] - 0.5
    out[r0:r1, c0:c1] = sub.reshape(rr.shape)
    return out


def _heli_flat(m, geom, col, lift, area=None):
    """Paint or slab lying on the apron: triangulated, each vertex at the
    apron's level plus lift."""
    n_ = 0
    for q in to_polys(geom):
        if q.area < 0.02:
            continue
        tr = triangulate(orient(q, 1.0), area)
        if tr is None:
            continue
        v, tri = tr
        s, t = heli_st(v[:, 0], v[:, 1])
        P = enu_fast(v[:, 0], v[:, 1], heli_z(s, t) + lift)
        m.tri(P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]], np.asarray(col, float), PLAIN)
        n_ += len(tri)
    return n_


def _st_poly(F, pts):
    return Polygon([F.xy(s, t) for s, t in pts])


def _heli_blue_from_photo(F, hi):
    """The light blue taxi lines' junctions between the stands, read off the
    20 cm photo (the straight runs are drawn exactly, below)."""
    if hi is None:
        return Polygon()
    ppm = hi.size[0] / (X1 - X0)
    T, s0, s1, t0, t1 = 10.0, -30.0, 137.0, -15.0, -1.0
    w, h = int((s1 - s0) * T), int((t1 - t0) * T)
    cx = (F.O[0] + s0 * F.u[0] + t0 * F.v[0] - X0) * ppm
    cy = (Y1 - (F.O[1] + s0 * F.u[1] + t0 * F.v[1])) * ppm
    coef = (F.u[0] * ppm / T, F.v[0] * ppm / T, cx, -F.u[1] * ppm / T, -F.v[1] * ppm / T, cy)
    a = np.asarray(hi.transform((w, h), Image.AFFINE, coef, resample=Image.BICUBIC)).astype(np.float32)
    a = ndimage.gaussian_filter(a, (1.0, 1.0, 0))
    r_, g_, b_ = a[..., 0], a[..., 1], a[..., 2]
    blue = (b_ - r_ > 14) & (b_ >= g_ - 2) & (g_ - r_ > 4)
    blue = ndimage.binary_closing(ndimage.binary_opening(blue, iterations=2), iterations=2)
    lab, n = ndimage.label(blue)
    if not n:
        return Polygon()
    sz = ndimage.sum(blue, lab, range(1, n + 1))
    blue = np.isin(lab, np.nonzero(sz > 4.0 * T * T)[0] + 1)     # pieces over 4 m2
    jj, ii = np.nonzero(blue)
    if not len(jj):
        return Polygon()
    cells = unary_union([box(s0 + i / T, t0 + j / T, s0 + (i + 1) / T, t0 + (j + 1) / T) for j, i in zip(jj, ii)])
    cells = cells.buffer(0.25, join_style=1).buffer(-0.5, join_style=1).buffer(0.25, join_style=1).simplify(0.08)
    return unary_union([_st_poly(F, q.exterior.coords) for q in to_polys(cells) if q.area > 2.0])


def _arrow(sa, ta, d, r0, r1, half, head_len, head_half):
    """A shaft from r0 to r1 along d from (sa, ta), with a head at r1."""
    n = np.array([-d[1], d[0]])
    c = np.array([sa, ta])
    p0, p1 = c + d * r0, c + d * (r1 - head_len)
    tip = c + d * r1
    return unary_union([Polygon([p0 - n * half, p1 - n * half, p1 + n * half, p0 + n * half]),
                        Polygon([p1 - n * head_half, tip, p1 + n * head_half])])


def heli_markings(F, hi):
    """The paint on the apron, in (s, t): {colour: geometry}."""
    out = {k: [] for k in ("yellow", "white", "blue")}
    ca, sa_ = math.cos(math.radians(HELI_ARM_DEG)), math.sin(math.radians(HELI_ARM_DEG))
    for name, s, t, R, ri, col, arms in HELI_PADS:
        c = Point(s, t)
        out[col].append(c.buffer(R, 96).difference(c.buffer(R - 0.4, 96)))
        out[col].append(c.buffer(ri, 64).difference(c.buffer(ri - 0.35, 64)))
        for ds, dt, arrow in arms:
            d = np.array([ds * ca, dt * sa_])
            if arrow == "out":
                out[col].append(_arrow(s, t, d, R - 0.2, R + 2.2, 0.35, 1.0, 0.75))
            elif arrow:
                out[col].append(_arrow(s, t, d, ri - 0.1, 10.6, 0.42, 1.9, 1.15))
            else:
                out[col].append(_arrow(s, t, d, ri - 0.1, R - 0.2, 0.42, 0.0, 0.42))
        if name == "2":
            # The H, upright to the north as painted.
            nth = np.array([F.u[1], F.v[1]])          # north in (s, t)
            nth = nth / np.linalg.norm(nth)
            est = np.array([nth[1], -nth[0]])
            def rect(a0, a1, b0, b1):
                return Polygon([np.array([s, t]) + est * a + nth * b for a, b in ((a0, b0), (a1, b0), (a1, b1), (a0, b1))])
            out[col] += [rect(-1.05, -0.55, -1.45, 1.45), rect(0.55, 1.05, -1.45, 1.45), rect(-0.55, 0.55, -0.22, 0.22)]
    # Blue taxi lines: along the back of the stands, down to the platforms.
    w = 0.45
    s0, s1 = HELI_TAXI_S
    for sc, se in ((HELI_JUTS[0], s0), (HELI_JUTS[1], s1)):
        corner = HELI_TAXI_T + abs(se - sc)
        line = LineString([(se, HELI_TAXI_T), (sc, corner), (sc, HELI_PADS[6 if sc < 0 else 7][2] - 6.1)])
        out["blue"].append(line.buffer(w, cap_style=2, join_style=2))
    out["blue"].append(LineString([(s0, HELI_TAXI_T), (s1, HELI_TAXI_T)]).buffer(w, cap_style=2))
    # Hatched ends: yellow stripes on the grey, between the line and the
    # buildings.
    for sc, se in ((HELI_JUTS[0], s0), (HELI_JUTS[1], s1)):
        lo, hi_ = (HELI_ENDS[0] + 2.0, se - 0.5) if sc < 0 else (se + 0.5, HELI_ENDS[1] - 1.0)
        zone = box(lo, -12.0, hi_, 7.0)
        inner = Polygon([(se, HELI_TAXI_T - 5), (se, HELI_TAXI_T), (sc, HELI_TAXI_T + abs(se - sc)), (sc, 9.0),
                         (se + (40 if sc < 0 else -40), 9.0), (se + (40 if sc < 0 else -40), HELI_TAXI_T - 5)])
        zone = zone.difference(inner.buffer(0.9, join_style=2))
        zone = zone.difference(LineString([(sc, -20), (sc, 9)]).buffer(1.1, cap_style=2))
        k = np.arange(-40, 40) * 1.6
        stripes = unary_union([Polygon([(lo + a, 7.0), (lo + a + 0.75, 7.0), (lo + a + 0.75 + 19.0, -12.0),
                                        (lo + a + 19.0, -12.0)]) for a in k])
        out["yellow"].append(zone.intersection(stripes))
    res = {}
    for k, v in out.items():
        g = unary_union([q for q in v if not q.is_empty])
        res[k] = unary_union([_st_poly(F, q.exterior.coords).difference(
            unary_union([_st_poly(F, h.coords) for h in q.interiors])) for q in to_polys(g)])
    res["blue"] = unary_union([res["blue"], _heli_blue_from_photo(F, hi)]).difference(
        unary_union([res["yellow"], res["white"]]).buffer(0.15))
    return res


def helicopter(m, x, y, z, heading, body="#c4232b", trim="#f2f1ec"):
    """A small single-engine helicopter (an AS350 / EC130 shape): cabin,
    tail boom with fin, three-blade rotor, skids. heading: radians, east = 0."""
    d = np.array([math.cos(heading), math.sin(heading)])
    n = np.array([-d[1], d[0]])
    body_c, trim_c, glass = (np.array(hexrgb(c), float) for c in (body, trim, "#27323a"))
    dark = np.array(hexrgb("#2b2d30"), float)

    def at(a, b, c):
        p = np.array([x, y]) + np.outer(np.atleast_1d(a), d) + np.outer(np.atleast_1d(b), n)
        return enu_fast(p[:, 0], p[:, 1], z + np.atleast_1d(c))
    # Cabin: a squashed egg, glazed at the front, white below.
    v, tri = unit_sphere(14, 8)
    L, W, H = 2.3, 0.85, 0.82
    a, b, c = v[:, 0] * L, v[:, 1] * W, v[:, 2] * H
    a = np.where(a > 0, a, a * 0.9)
    c = np.where(c < 0, c * 0.8, c)
    cols = np.where(((a > 0.9) & (c > -0.15))[:, None], glass, np.where((c < -0.35)[:, None], trim_c, body_c))
    P = at(a, b, c + 1.35)
    Q = at(a * 1.1, b * 1.1, c * 1.1 + 1.35)
    nrm = Q - P
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
    m.add(P, nrm, cols, PLAIN, idx=tri)
    F = Frame(m, (x, y), d)              # s forward, t to the right of the nose
    # Engine cowling and mast.
    F.box(-1.2, 0.4, -0.45, 0.45, z + 1.9, z + 2.35, body_c)
    _cyl(m, x, y, z + 2.35, z + 2.75, 0.12, dark, 8)
    # Tail boom, tapering, and the fin with its small tailplane.
    for i in range(6):
        s_a, s_b = -2.0 - i * 0.85, -2.0 - (i + 1) * 0.85
        hw = 0.28 - 0.025 * i
        F.box(s_b, s_a, -hw, hw, z + 1.45 - 0.02 * i, z + 1.45 + 2 * hw - 0.02 * i, body_c if i < 4 else trim_c)
    F.box(-7.4, -6.9, -0.06, 0.06, z + 1.4, z + 2.85, body_c)
    F.box(-6.6, -6.1, -1.1, 1.1, z + 1.5, z + 1.58, trim_c)
    # Rotor: three blades, one along the boom as they park.
    for k in range(3):
        ang = heading + math.pi + k * 2 * math.pi / 3
        e = np.array([math.cos(ang), math.sin(ang)])
        Fb = Frame(m, (x, y), e)
        Fb.box(0.15, 5.35, -0.17, 0.17, z + 2.74, z + 2.8, dark, bottom=True)
    # Skids on struts.
    for sd in (-1.05, 1.05):
        F.box(-1.6, 1.9, sd - 0.06, sd + 0.06, z + 0.05, z + 0.17, dark)
        for sa in (-0.9, 0.9):
            F.box(sa - 0.07, sa + 0.07, sd * 0.55 - 0.05, sd * 0.55 + 0.05, z + 0.12, z + 0.75, dark)
            F.box(sa - 0.07, sa + 0.07, min(sd, sd * 0.55) - 0.03, max(sd, sd * 0.55) + 0.03, z + 0.12, z + 0.2, dark)


def heliport(gm, m, hi, stats):
    """The apron, its paint, edges, nets and platforms; the control tower;
    a parked helicopter."""
    if HELI is None:
        return 0
    F = Frame(m, HELI["F"].O, HELI["F"].u)
    deck = HELI["deck"]
    conc = np.array(hexrgb(HELI_PAINT["concrete"]), float)
    face = np.array(hexrgb(HELI_PAINT["face"]), float)
    # The concrete, a shade darker on the platforms (newer slabs, photo).
    n_ = _heli_flat(m, HELI["main"], conc, 0.0, 8.0)
    for j in HELI["juts"]:
        n_ += _heli_flat(m, j, conc * 0.94, 0.0, 4.0)
    paint = heli_markings(F, hi)
    for k, g in paint.items():
        n_ += _heli_flat(m, g.intersection(deck.buffer(-0.05)), hexrgb(HELI_PAINT[k]), 0.03, 0.8)
    # Red and white edge: along the sea edge between the platforms and
    # round the platforms (the viewer's red and white kerb paint).
    def kerb_band(A, B, inward, width):
        A, B = np.asarray(A, float), np.asarray(B, float)
        L = np.linalg.norm(B - A, axis=1)
        s0 = np.r_[0.0, np.cumsum(L)[:-1]]
        s1 = s0 + L
        Ai, Bi = A + inward * width, B + inward * width
        za = heli_z(*heli_st(A[:, 0], A[:, 1])) + 0.035
        zb = heli_z(*heli_st(B[:, 0], B[:, 1])) + 0.035
        zero, one = np.zeros(len(A)), np.ones(len(A))
        up3 = np.tile([0.0, 0.0, 1.0], (len(A), 1))
        _quad_uv(m, enu_fast(A[:, 0], A[:, 1], za), enu_fast(B[:, 0], B[:, 1], zb), enu_fast(Bi[:, 0], Bi[:, 1], zb),
                 enu_fast(Ai[:, 0], Ai[:, 1], za), np.array(hexrgb("#e9e7e1")), up3,
                 [np.column_stack([s0, zero]), np.column_stack([s1, zero]), np.column_stack([s1, one]),
                  np.column_stack([s0, one])], style=KERB_RW)
        return float(L.sum())
    a_, b_ = HELI_JUTS[0] + HELI_JUT_HALF, HELI_JUTS[1] - HELI_JUT_HALF
    ss = np.linspace(a_, b_, int((b_ - a_) / 2.0) + 1)
    A = np.array([F.xy(s, HELI_EDGE - 0.02) for s in ss])
    red_m = kerb_band(A[:-1], A[1:], -F.v, 1.2)
    for j in HELI["juts"]:
        ring = np.asarray(orient(j, 1.0).exterior.coords)[:, :2]
        for p, q in zip(ring[:-1], ring[1:]):
            mid = (p + q) / 2
            if heli_st(*mid)[1][0] < HELI_EDGE + 0.5:
                continue                              # the side on the apron
            e = (q - p) / np.linalg.norm(q - p)
            k = max(1, int(np.linalg.norm(q - p) / 2.0))
            pts = p + np.outer(np.linspace(0, 1, k + 1), q - p)
            red_m += kerb_band(pts[:-1], pts[1:], np.array([-e[1], e[0]]), 0.7)
    stats["heliport_red_white_m"] = round(red_m)
    # Sea edge: a concrete face down to the quay, the safety net frames
    # sloping out over it on ribs, posts under the outer rail.
    steel = np.array(hexrgb(HELI_PAINT["steel"]), float)
    for s_a, s_b in zip(ss[:-1], ss[1:]):
        za, zb = heli_z(s_a, HELI_EDGE), heli_z(s_b, HELI_EDGE)
        F.quad((s_a, HELI_EDGE, HELI_QUAY - 0.3), (s_b, HELI_EDGE, HELI_QUAY - 0.3), (s_b, HELI_EDGE, float(zb)),
               (s_a, HELI_EDGE, float(za)), face, (0, 1, 0))
    for s_a, s_b in zip(ss[:-1], ss[1:]):
        za, zb = float(heli_z(s_a, HELI_EDGE)) - 0.15, float(heli_z(s_b, HELI_EDGE)) - 0.15
        P = [F.E(s_a, HELI_EDGE, za), F.E(s_b, HELI_EDGE, zb), F.E(s_b, HELI_EDGE + HELI_NET, zb - 1.0),
             F.E(s_a, HELI_EDGE + HELI_NET, za - 1.0)]
        uv = [np.array([[s_a, 0.0]]), np.array([[s_b, 0.0]]), np.array([[s_b, 1.0]]), np.array([[s_a, 1.0]])]
        for sg in (1.0, -1.0):
            _quad_uv(m, *P, np.array(hexrgb(HELI_PAINT["net"])), F.want(s_a, HELI_EDGE + 1, za, 0, 0, sg), uv, style=RAIL)
    rib = np.arange(a_ + 1.1, b_ - 0.5, 2.2)
    for s in rib:
        z0 = float(heli_z(s, HELI_EDGE)) - 0.15
        for k_ in range(4):
            t0_, t1_ = HELI_EDGE + k_ * HELI_NET / 4, HELI_EDGE + (k_ + 1) * HELI_NET / 4
            F.box(s - 0.06, s + 0.06, t0_, t1_, z0 - 1.0 * (k_ + 1) / 4 - 0.12, z0 - 1.0 * k_ / 4 + 0.02, steel)
    zo = heli_z(ss, HELI_EDGE) - 1.15
    for s_a, s_b, za, zb in zip(ss[:-1], ss[1:], zo[:-1], zo[1:]):
        F.box(s_a, s_b, HELI_EDGE + HELI_NET - 0.08, HELI_EDGE + HELI_NET + 0.08, float(min(za, zb)) - 0.12,
              float(max(za, zb)) + 0.04, steel, bottom=True)
    for s in rib[::2]:
        x, y = F.xy(s, HELI_EDGE + HELI_NET - 0.1)
        _cyl(m, x, y, HELI_QUAY - 0.1, float(heli_z(s, HELI_EDGE)) - 1.2, 0.1, steel * 0.8, 6)
    # The platforms: faces down to the water, on piers.
    for sc, j in zip(HELI_JUTS, HELI["juts"]):
        ring = np.asarray(orient(j, 1.0).exterior.coords)[:, :2]
        for p, q in zip(ring[:-1], ring[1:]):
            mid = (p + q) / 2
            if heli_st(*mid)[1][0] < HELI_EDGE + 0.5:
                continue
            sp, tp = heli_st(*p)
            sq, tq = heli_st(*q)
            zp, zq = float(heli_z(sp, tp)[0]), float(heli_z(sq, tq)[0])
            nn = np.array([(q - p)[1], -(q - p)[0]])
            F.quad((float(sp[0]), float(tp[0]), zp - 1.1), (float(sq[0]), float(tq[0]), zq - 1.1),
                   (float(sq[0]), float(tq[0]), zq), (float(sp[0]), float(tp[0]), zp), face,
                   (float(nn @ F.u), float(nn @ F.v), 0))
            # A white railing at the platform's edge.
            ins = -nn / np.linalg.norm(nn) * 0.15
            railing(m, np.array([p + ins]), np.array([q + ins]), np.array([zp]), "#f1f0ea", 1.0, zb=np.array([zq]))
        jz = HELI["jut_z"][0 if sc < 0 else 1]
        for ps in (-HELI_JUT_HALF + 1.2, 0.0, HELI_JUT_HALF - 1.2):
            for pt in (HELI_EDGE + HELI_NET + 1.0, (HELI_EDGE + HELI_JUT_END) / 2 + 2.0, HELI_JUT_END - 1.2):
                x, y = F.xy(sc + ps, pt)
                _cyl(m, x, y, -1.0, jz - 1.1, 0.45, face * 0.9, 10)
        # Under the deck slab: its soffit.
        sl = j.difference(F.poly(sc - 20, sc + 20, HELI_EDGE - 5, HELI_EDGE + HELI_NET))
        for q in to_polys(sl):
            tr = triangulate(orient(q, 1.0))
            if tr is None:
                continue
            v, tri = tr
            P = enu_fast(v[:, 0], v[:, 1], np.full(len(v), jz - 1.1))
            m.tri(P[tri[:, 0]], P[tri[:, 2]], P[tri[:, 1]], face * 0.7, PLAIN)
    # Control tower on the terminal's corner: a shaft and a glazed cab.
    ts, tt = HELI_TOWER
    zr = float(heli_z(ts, -12.0)) + 4.1               # the terminal's roof (LiDAR, 9.9 m)
    cream = np.array(hexrgb("#efe4cf"), float)
    F.drum(ts, tt, zr - 0.5, HELI_TOWER_TOP - 3.0, 1.7, cream, n=8, th0=math.pi / 8, th1=math.pi / 8 + 2 * math.pi)
    F.drum(ts, tt, HELI_TOWER_TOP - 3.0, HELI_TOWER_TOP - 0.7, 2.2, np.array(hexrgb("#4a5a66"), float), n=8,
           th0=math.pi / 8, th1=math.pi / 8 + 2 * math.pi, style=FORUM_GLASS)
    F.drum(ts, tt, HELI_TOWER_TOP - 3.2, HELI_TOWER_TOP - 3.0, 2.35, cream, n=8, th0=math.pi / 8,
           th1=math.pi / 8 + 2 * math.pi)
    F.drum(ts, tt, HELI_TOWER_TOP - 0.7, HELI_TOWER_TOP - 0.3, 2.6, cream, n=8, th0=math.pi / 8,
           th1=math.pi / 8 + 2 * math.pi)
    F.cone(ts, tt, HELI_TOWER_TOP - 0.3, 2.6, 0.3, cream, n=8, th0=math.pi / 8, th1=math.pi / 8 + 2 * math.pi, kind=PLAIN)
    x, y = F.xy(ts, tt)
    _cyl(m, x, y, HELI_TOWER_TOP, HELI_TOWER_TOP + 2.5, 0.05, np.array(hexrgb("#d0d0d0"), float), 6)
    # Windsock by platform B (orange and white).
    xw, yw = F.xy(HELI_JUTS[1] + HELI_JUT_HALF - 0.6, HELI_JUT_END - 3.4)
    jzb = HELI["jut_z"][1]
    _cyl(m, xw, yw, jzb, jzb + 5.2, 0.06, steel, 6)
    Fw = Frame(m, (xw, yw), (-F.u[0], -F.u[1]))
    for i in range(5):
        r_ = 0.32 - 0.04 * i
        Fw.drum(0.25 + 0.5 * i, 0.0, jzb + 4.9 - 0.05 * i - r_, jzb + 4.9 - 0.05 * i + r_, r_,
                np.array(hexrgb("#e2672c" if i % 2 == 0 else "#f2f1ec"), float), n=8)
    # The helicopter on stand 5, as in the photo.
    for name, hd in HELI_PARKED:
        p_ = next(p for p in HELI_PADS if p[0] == name)
        x, y = F.xy(p_[1], p_[2])
        ang = math.radians(HELI_AXIS + hd)
        helicopter(m, x, y, float(heli_z(p_[1], p_[2])) + 0.03, ang)
    stats["heliport_tris"] = n_
    stats["heliport_m2"] = round(deck.area)
    return n_


def heli_hangar_doors(m, poly, base, H):
    """Hangar doors on the apron side: tall panels in a row, a shade darker
    than the cladding."""
    if HELI is None:
        return 0
    F = HELI["F"]
    ring = np.asarray(orient(poly, 1.0).exterior.coords)[:, :2]
    k = 0
    for p, q in zip(ring[:-1], ring[1:]):
        e = q - p
        L = float(np.linalg.norm(e))
        if L < 8:
            continue
        e /= L
        out = np.array([e[1], -e[0]])
        if out @ F.v < 0.8:
            continue
        Fd = Frame(m, p, e)          # s along the wall, t out of it
        n = max(1, int(round((L - 1.0) / 4.6)))
        w = (L - 1.0) / n
        top = min(base + 6.0, base + H - 1.0)
        for i in range(n):
            a = 0.5 + i * w
            Fd.box(a + 0.06, a + w - 0.06, -0.02, 0.16, base, top, np.array(hexrgb("#b9c0c6"), float))
            Fd.box(a + 0.3, a + w - 0.3, 0.16, 0.2, base + 3.4, base + 3.9, np.array(hexrgb("#8f989f"), float))
        Fd.box(0.3, L - 0.3, -0.02, 0.35, top, top + 0.45, np.array(hexrgb("#d6d6d2"), float), bottom=True)
        k += n
    return k


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
    global ALLEE_FRONT
    al_ = allee_line(ways)
    if al_ is not None and al_.length > 40:
        # From the square down to where the gardens begin.
        ALLEE_FRONT = shapely.ops.substring(al_, 24.0, al_.length)
    print("real shops:", len(SHOPS["pts"]), "points,", len(SHOPS["areas"]), "areas")
    # Yachts moored against a quay merge with it in the LiDAR terrain. The
    # OpenStreetMap coastline follows the quays, so anything outside it is sea.
    land = coast_land(ways, sea)
    sea |= ~land
    global MT
    MT = mareterra_setup(ways, mnt, mns, ortho, sea, land)
    global PORTIER
    PORTIER = portier_setup(ways, mnt, mns, ortho, sea)
    global ARMES, CONDA, ROCHER_GARDENS, RAMPE
    ARMES = armes_setup(ways)
    RAMPE = rampe_setup(ways)
    rampe_heights(mnt, ways)
    CONDA = condamine_setup(ways)
    ROCHER_GARDENS = rocher_gardens_setup(ways)
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
    if SW is not None and PORTIER is not None:
        # The roundabout's plaza is pale stone, not the circuit's red brick.
        for k_ in ("poly", "pale", "islands"):
            if k_ in SW:
                SW[k_] = SW[k_].difference(PORTIER["zone"])
    if SW is not None and mnt_ground is not mnt_f:
        m_ = raster_mask(SW["poly"])
        mnt_ground[m_] = mnt_f[m_]
    portals_setup(ways, mnt_f)
    global FM, STADE_POLY
    FM = fairmont_setup(feats, mns, mnt_f)
    STADE_POLY = stade_setup(feats)
    print("heliport apron…", heliport_setup(ways, mnt_f, feats))
    mnt_pre = mnt_f
    mnt_f, mns, wq_m2 = wq_flatten(mnt_f, mns)
    mnt_ground = np.where(np.nan_to_num(mnt_f, nan=-99) != np.nan_to_num(mnt_pre, nan=-99), mnt_f, mnt_ground)
    print("west quays…", wq_setup(ways, mnt_f, sea), "m2 of quay band;", wq_m2, "m2 of yard humps levelled")
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
    yachts(hm, ortho, sea, stats, ways, ortho_hi)
    piers(hm, ways, sea)
    print("quay walls…", quay_walls(hm, sea, mnt_f, stats))
    stats["bridges"] = bridges(bm, ways, mns, mnt)
    stats["diving_towers"] = pool_features(bm, ways, mnt_f)
    print("Hôtel Métropole…", metropole(bm, ways, Raster(mnt_f), mns, stats))
    sm = Mesh()
    sea_plane(sm)

    print("ground…")
    chm = np.nan_to_num(mns - mnt_f, nan=0)
    canopy = ((chm > 2.5) & ~bgrow).astype(np.float32)
    canopy = ndimage.zoom(canopy, 0.5, order=1)
    print("Fontvieille podiums…", podium_setup(ways, mnt_f))
    gm, gz = ground_mesh(solarium_ground(wq_ground(heliport_ground(podium_ground(mnt_ground)))), sea)
    print("podium walls…", podium_walls(gm, bm, stats), "m")
    print("heliport…", heliport(gm, bm, ortho_hi, stats), "triangles of apron and paint")
    print("west quays…", wq_build(gm, bm, sea, stats), "m of quay wall")
    print("tunnel portals…", portals(gm, bm, stats))
    rm = Mesh()
    print("roads…", road_surfaces(rm, ways, pts, gz, bmask, stats), "m")
    drive = drive_map(gz, bmask, sea)
    stats["lamps"] = street_lamps(bm, ways, gz, bmask, sea)
    print("crowds…", crowds(bm, mnt_f, sea, bmask, stats))
    if SW is None:
        pavements(bm, sea, bmask, stats)
    print("sidewalks…", sidewalks(gm, bm, stats))
    print("Allée François Blanc…", allee_francois_blanc(gm, bm, ways, pts, mnt_f, fps, stats, gz), "paved pieces,",
          stats.get("allee_shops"), "shops")
    print("Casino apron…", casino_apron(gm, ways, fps, gz, stats), "pieces,", stats.get("casino_apron_m2"), "m2")
    print("Café de Paris terrace…", cafe_terrace(bm, Raster(mnt_f), ways, pts, stats), "parasols")
    print("street walls…", street_walls(gm, bm, stats))
    print("car park doors…", garage_doors(bm, entrances, fps, stats))
    print("starting grid…", start_grid(bm, stats), "slots")
    ostende_promenade(bm, mnt_f, stats)
    print("street features…", street_features(bm, ways, pts, mnt_f, stats))
    print("Quai Albert 1er…", quai_albert(gm, bm, tm, ways, pts, Raster(mnt_f), stats, fps), "pines")
    print("Fairmont…", fairmont(gm, bm, tm, mns, mnt_f, sea, stats), "balconies,", stats.get("fairmont_columns"), "columns,",
          stats.get("fairmont_roof_trees"), "roof trees")
    print("Place d'Armes…", place_darmes(gm, bm, tm, ways, pts, mns, mnt_f, ortho, ortho_hi, gz, fps, stats), "stalls")
    print("Rampe Major…", rampe_major(gm, bm, ways, mnt_f, stats), "steps,", stats.get("rampe_major_parapet_m"), "m of parapet")
    print("Le Rocher trees…", rocher_trees(tm, mns, mnt_f, ortho, fps, stats))
    print("Le Rocher ramparts…", rocher_ramparts(bm, ways, pts, mnt_f, stats), "m of wall,",
          stats.get("rampart_cannons"), "cannons")
    print("Le Rocher gardens…", rocher_gardens(gm, bm, ways, pts, mnt_f, gz, stats), "benches,",
          stats.get("rocher_garden_path_m2"), "m2 of paths")
    print("Le Rocher streets…", rocher_streets(gm, ways, mnt_f, gz, fps, stats), "m2 of setts,",
          stats.get("rocher_brick_m2"), "m2 of brick pavement")
    print("Pêcheurs garden…", peche_terrace(gm, bm, mnt_f, gz, stats), "m of sea wall,",
          stats.get("peche_walk_m2"), "m2 of walk")
    print("Digue…", digue(bm, tm, Raster(mnt_f), stats), "trees,", stats.get("digue_m"), "m of breakwater")
    print("La Condamine…", condamine(gm, bm, tm, ways, pts, mns, mnt_f, ortho, gz, fps, stats), "orange trees")
    print("parks…", parks(gm, bm, ways, pts, mnt_f, mns, ortho_hi, gz, fps, stats), "parks and gardens")
    jpg = paint_ground(ways, mnt_f, ndimage.zoom(wq_sea_paint(sea).astype(np.float32), 0.5, order=1),
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
