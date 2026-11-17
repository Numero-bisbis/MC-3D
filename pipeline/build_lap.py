#!/usr/bin/env python3
"""Bake the Circuit de Monaco route for the "Grand Prix lap" tour.

The route is the OpenStreetMap relation 148194 ("Circuit de Monaco"): its road
segments are chained in driving order from the start/finish line, smoothed a
little (OSM draws corners as short straight pieces) and resampled every 2 m.
Each point gets two heights from the IGN LiDAR HD models:
  - ground: the bare road (terrain model, MNT);
  - clear:  the highest thing over the road within 3.5 m (surface model, MNS:
            tree crowns, footbridges), so the camera can stay above it.
In the tunnel the models only know the roofs above, so both heights run
straight from one portal to the other.

Output: viewer/lap-route.js (committed, so the site build does not need
OpenStreetMap). Run it again only if the route or the height data change.

Usage:
  python pipeline/build_lap.py
"""
import json
import math
import sys
import urllib.request
from pathlib import Path

import numpy as np
import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_surface import load_mosaic  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
RELATION = 148194
START_NODE = 4937755860      # "Depart Grand Prix de Monaco"
# Road segments (OSM way ids) in driving order; a minus sign means the way is
# drawn against the racing direction. Pit lane and pit exit are left out.
ORDER = [
    166399479, 1453878835, 1081401616, 157719644, 254596486, 1551240830,
    1551240829, 161775592, -1082515450, -161752645, -4229658, 4230009, 4230006,
    434567309, 166399501, -568187257, -4230007, 1148675745, 1470365906,
    1470365907, 1148199871, -41929969, -4230891, -1230247123, -160004393,
    -4229536, -1451501763, -348019480, 1081401613, -485746484, 503475642,
    1081401614, 214636589, 1081401615, -39839529, 1081401617, 168681959,
    60767229, 4226740, 1019174508,
]
STEP = 2.0            # m between baked points
SMOOTH = 3.0          # m, Gaussian sigma applied to the centre line
CORRIDOR = 3.5        # m, radius searched for things above the road
MIN_TUNNEL = 100.0    # m; shorter covered bits (one overpass) are flown over


def fetch_relation():
    cache = RAW / "lap" / f"relation_{RELATION}.json"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://api.openstreetmap.org/api/0.6/relation/{RELATION}/full.json"
        req = urllib.request.Request(url, headers={"User-Agent": "monaco-3d-pipeline"})
        cache.write_bytes(urllib.request.urlopen(req, timeout=60).read())
    return json.loads(cache.read_text())


def chain(data):
    """Ordered (lon, lat, in_tunnel) points of one lap, from the start line."""
    nodes = {e["id"]: e for e in data["elements"] if e["type"] == "node"}
    ways = {e["id"]: e for e in data["elements"] if e["type"] == "way"}
    seq = []
    for o in ORDER:
        w = ways[abs(o)]
        ids = w["nodes"] if o > 0 else w["nodes"][::-1]
        tunnel = w.get("tags", {}).get("tunnel") == "yes"
        for n in ids:
            if seq and seq[-1][0] == n:
                continue
            seq.append((n, tunnel))
    if seq[0][0] != seq[-1][0]:
        raise SystemExit("route is not a closed loop any more; update ORDER")
    seq.pop()
    i0 = next(i for i, s in enumerate(seq) if s[0] == START_NODE)
    seq = seq[i0:] + seq[:i0]
    return [(nodes[n]["lon"], nodes[n]["lat"], t) for n, t in seq]


def gauss_wrap(a, sigma_samples):
    """Gaussian smoothing of a closed loop (values wrap around)."""
    r = int(4 * sigma_samples) + 1
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma_samples) ** 2)
    k /= k.sum()
    padded = np.concatenate([a[-r:], a, a[:r]])
    return np.convolve(padded, k, mode="valid")


def main():
    pts = chain(fetch_relation())
    to_l93 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    to_wgs = pyproj.Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    x, y = to_l93.transform([p[0] for p in pts], [p[1] for p in pts])
    xy = np.column_stack([x, y])
    tun = np.array([p[2] for p in pts])

    # Dense 1 m resampling of the closed polygon, then smoothing, then 2 m.
    closed = np.vstack([xy, xy[:1]])
    seg = np.hypot(*np.diff(closed, axis=0).T)
    cum = np.concatenate([[0], np.cumsum(seg)])
    s1 = np.arange(0, cum[-1], 1.0)
    dense = np.column_stack([np.interp(s1, cum, closed[:, k]) for k in (0, 1)])
    # A dense point is in a tunnel when the OSM segment it sits on is one.
    seg_tunnel = tun & np.roll(tun, -1)
    dense_tunnel = seg_tunnel[np.clip(np.searchsorted(cum, s1, side="right") - 1, 0, len(seg) - 1)]
    smooth = np.column_stack([gauss_wrap(dense[:, k], SMOOTH) for k in (0, 1)])
    print("largest shift from smoothing: %.2f m" % np.hypot(*(smooth - dense).T).max())

    closed = np.vstack([smooth, smooth[:1]])
    seg = np.hypot(*np.diff(closed, axis=0).T)
    cum2 = np.concatenate([[0], np.cumsum(seg)])
    length = cum2[-1]
    n = int(round(length / STEP))
    s = np.arange(n) * (length / n)
    px = np.interp(s, cum2, closed[:, 0])
    py = np.interp(s, cum2, closed[:, 1])
    in_tunnel = dense_tunnel[np.clip(np.round(np.interp(s, cum2[:-1], s1)).astype(int), 0, len(s1) - 1)]

    # Heights from the LiDAR models (IGN69 altitudes, 50 cm grid).
    names = [l.strip() for l in open(ROOT / "pipeline" / "tiles.txt") if l.strip() and not l.startswith("#")]
    mnt, x0, y0 = load_mosaic(names, "mnt")
    mns, _, _ = load_mosaic(names, "mns")
    col = np.round((px - x0) / 0.5).astype(int)
    row = np.round((y0 - py) / 0.5).astype(int)
    r = int(CORRIDOR / 0.5)
    ground = np.empty(n)
    clear = np.empty(n)
    for i in range(n):
        g = mnt[row[i] - 2:row[i] + 3, col[i] - 2:col[i] + 3]
        ground[i] = np.nanmedian(g)
        clear[i] = np.nanmax(mns[row[i] - r:row[i] + r + 1, col[i] - r:col[i] + r + 1])

    # Tunnels: keep only long ones, and run the heights portal to portal.
    tunnels = []
    i = 0
    while i < n:
        if in_tunnel[i]:
            j = i
            while j + 1 < n and in_tunnel[j + 1]:
                j += 1
            if (j - i) * STEP >= MIN_TUNNEL:
                tunnels.append((i, j))
            i = j + 1
        else:
            i += 1
    for a, b in tunnels:
        a0, b0 = a - 3, b + 3          # a few metres of open road each side
        ground[a0:b0 + 1] = np.linspace(ground[a0], ground[b0], b0 - a0 + 1)
        clear[a0:b0 + 1] = ground[a0:b0 + 1]
    ground = gauss_wrap(ground, 6.0 / STEP)
    clear = np.maximum(clear, ground)

    # IGN69 altitude -> WGS84 ellipsoid height with the RAF20 geoid grid.
    pyproj.datadir.append_data_dir(str(RAW / "grids"))
    g = pyproj.transformer.TransformerGroup("EPSG:2154+5720", "EPSG:4979", always_xy=True)
    if not g.transformers or "RAF20" not in g.transformers[0].description.upper() + g.transformers[0].definition.upper():
        print("warning: RAF20 grid not found, using 48.9 m", file=sys.stderr)
        geoid = 48.9
    else:
        _, _, h = g.transformers[0].transform(px.mean(), py.mean(), 0.0)
        geoid = float(h)
    print("geoid N %.2f m, lap %.1f m, %d points, tunnels %s" % (
        geoid, length, n, [(round(a * length / n), round(b * length / n)) for a, b in tunnels]))

    lon, lat = to_wgs.transform(px, py)
    out = {
        "source": f"OpenStreetMap relation {RELATION} (ODbL) + IGN LiDAR HD heights",
        "length": round(float(length), 1),
        "step": round(float(length / n), 4),
        "tunnels": [[round(a * length / n, 1), round(b * length / n, 1)] for a, b in tunnels],
        # [lon, lat, ground height, clear height], heights above the WGS84 ellipsoid
        "points": [[round(float(lon[i]), 7), round(float(lat[i]), 7),
                    round(float(ground[i] + geoid), 1), round(float(clear[i] + geoid), 1)] for i in range(n)],
    }
    dst = ROOT / "viewer" / "lap-route.js"
    dst.write_text(
        "// Generated by pipeline/build_lap.py. Do not edit by hand.\n"
        "// Circuit de Monaco centre line: (c) OpenStreetMap contributors, ODbL.\n"
        "window.MC3D_LAP_ROUTE = " + json.dumps(out, separators=(",", ":")) + ";\n")
    print("wrote", dst, dst.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
