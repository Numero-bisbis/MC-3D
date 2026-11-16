#!/usr/bin/env python3
"""Turn IGN LiDAR HD tiles into a Cesium-ready 3D Tiles point cloud.

Steps for every 1 km LiDAR tile:
  1. read the classified COPC file, drop noise and artefact points;
  2. thin it to a point budget (the full data is ~430 M points for Monaco);
  3. colour each point from the IGN orthophoto of the same tile;
  4. convert Lambert-93 + NGF-IGN69 heights to WGS84 ellipsoid (RAF20 geoid
     grid), then to Earth-centred coordinates, then to a local east-north-up
     frame centred on Monaco;
  5. split into three levels of detail (1 km, 500 m, 250 m) with additive
     refinement and write quantized .pnts files plus a tileset.json.

Usage:
  python pipeline/build_tiles.py --out build/tiles --points-per-km2 1500000
"""
import argparse
import json
import math
import os
import struct
from pathlib import Path

import laspy
import numpy as np
import pyproj
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

# Monaco centre (Casino square area), used as the origin of the local frame.
CENTER_LON, CENTER_LAT = 7.4246, 43.7384

# ASPRS / IGN classes. 7 = noise, 18 = high noise, 9 = water (the sea is drawn
# by the viewer instead: water returns are sparse and look like static),
# 64-66 = IGN artefacts and virtual points.
DROP_CLASSES = {7, 9, 18, 64, 65, 66}

# Share of the kept points at each level of the 1 km tile.
LEVEL_SHARE = (0.10, 0.25, 0.65)  # 1 km node, 500 m nodes, 250 m nodes
ROOT_SAMPLE_PER_TILE = 12000


def make_transformer():
    pyproj.datadir.append_data_dir(str(RAW / "grids"))
    group = pyproj.transformer.TransformerGroup(
        "EPSG:2154+5720", "EPSG:4979", always_xy=True
    )
    for t in group.transformers:
        # Pick the operation that really applies the RAF geoid grid.
        if "NGF-IGN69 height" in t.description and "ballpark" not in t.description:
            return t
    raise SystemExit("RAF20 geoid grid not found: run pipeline/download.sh first")


def enu_frame(lon, lat):
    """Rotation (3x3) and origin (ECEF) of the east-north-up frame."""
    ecef = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    ox, oy, oz = ecef.transform(lon, lat, 0.0)
    lo, la = math.radians(lon), math.radians(lat)
    east = np.array([-math.sin(lo), math.cos(lo), 0.0])
    north = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    rot = np.vstack([east, north, up])  # rows: ECEF -> ENU
    return rot, np.array([ox, oy, oz])


def pad(b: bytes, fill: bytes = b" ", align: int = 8) -> bytes:
    return b + fill * ((-len(b)) % align)


def write_pnts(path, pos, rgb, cls):
    """Write a .pnts tile with 16-bit quantized positions, RGB and classification."""
    n = len(pos)
    lo = pos.min(axis=0)
    span = np.maximum(pos.max(axis=0) - lo, 1e-3)
    q = np.round((pos - lo) / span * 65535).astype("<u2")
    ft_bin = pad(q.tobytes(), b"\0")
    rgb_off = len(ft_bin)
    ft_bin = pad(ft_bin + rgb.astype("u1").tobytes(), b"\0")
    ft_json = {
        "POINTS_LENGTH": n,
        "POSITION_QUANTIZED": {"byteOffset": 0},
        "QUANTIZED_VOLUME_OFFSET": lo.tolist(),
        "QUANTIZED_VOLUME_SCALE": span.tolist(),
        "RGB": {"byteOffset": rgb_off},
    }
    bt_json = {"classification": {"byteOffset": 0, "componentType": "UNSIGNED_BYTE", "type": "SCALAR"}}
    bt_bin = pad(cls.astype("u1").tobytes(), b"\0")
    ftj = pad(json.dumps(ft_json, separators=(",", ":")).encode())
    btj = pad(json.dumps(bt_json, separators=(",", ":")).encode())
    total = 28 + len(ftj) + len(ft_bin) + len(btj) + len(bt_bin)
    header = struct.pack("<4sIIIIII", b"pnts", 1, total, len(ftj), len(ft_bin), len(btj), len(bt_bin))
    with open(path, "wb") as f:
        f.write(header + ftj + ft_bin + btj + bt_bin)
    return lo, lo + span


def box(lo, hi, margin=1.0):
    c = (lo + hi) / 2
    h = (hi - lo) / 2 + margin
    return {"box": [c[0], c[1], c[2], h[0], 0, 0, 0, h[1], 0, 0, 0, h[2]]}


def load_tile(name, budget, rng, to_geo, to_ecef, rot, origin):
    las = laspy.read(RAW / "lidar" / f"LHD_FXX_{name}_PTS_LAMB93_IGN69.copc.laz")
    cls = np.asarray(las.classification)
    keep = ~np.isin(cls, list(DROP_CLASSES)) & ~np.asarray(las.withheld, dtype=bool)
    idx = np.flatnonzero(keep)
    if budget < len(idx):
        idx = rng.choice(idx, size=budget, replace=False)
    rng.shuffle(idx)

    x = np.asarray(las.x)[idx]
    y = np.asarray(las.y)[idx]
    z = np.asarray(las.z)[idx]
    c = cls[idx]

    # Colour from the orthophoto covering exactly this 1 km tile.
    tx, ty = (int(v) for v in name.split("_"))
    img = np.asarray(Image.open(RAW / "ortho" / f"ortho_{name}.jpg").convert("RGB"))
    res = 1000.0 / img.shape[1]
    col = np.clip(((x - tx * 1000) / res).astype(int), 0, img.shape[1] - 1)
    row = np.clip(((ty * 1000 - y) / res).astype(int), 0, img.shape[0] - 1)
    rgb = img[row, col]

    lon, lat, h = to_geo.transform(x, y, z)
    ex, ey, ez = to_ecef.transform(lon, lat, h)
    enu = (np.column_stack([ex, ey, ez]) - origin) @ rot.T
    return x, y, enu, rgb, c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "build" / "tiles"))
    ap.add_argument("--points-per-km2", type=int, default=1_500_000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    to_geo = make_transformer()
    print("vertical datum:", to_geo.description)
    to_ecef = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    rot, origin = enu_frame(CENTER_LON, CENTER_LAT)

    names = [l.strip() for l in open(ROOT / "pipeline" / "tiles.txt") if l.strip() and not l.startswith("#")]
    tile_nodes, root_pos, root_rgb, root_cls = [], [], [], []
    total = 0

    for name in names:
        tx, ty = (int(v) for v in name.split("_"))
        x, y, enu, rgb, cls = load_tile(name, args.points_per_km2, rng, to_geo, to_ecef, rot, origin)
        n = len(enu)
        total += n
        print(f"{name}: {n:,} points")

        r = min(ROOT_SAMPLE_PER_TILE, n // 20)
        root_pos.append(enu[:r]); root_rgb.append(rgb[:r]); root_cls.append(cls[:r])
        enu, rgb, cls, x, y = enu[r:], rgb[r:], cls[r:], x[r:], y[r:]
        n = len(enu)

        a = int(n * LEVEL_SHARE[0])
        b = a + int(n * LEVEL_SHARE[1])
        lx = x - tx * 1000          # 0..1000 east
        ly = ty * 1000 - y          # 0..1000 south

        def node(sel, file, err, children):
            lo, hi = write_pnts(out / file, enu[sel], rgb[sel], cls[sel])
            return {"boundingVolume": box(lo, hi), "geometricError": err, "refine": "ADD",
                    "content": {"uri": file}, "children": children,
                    "_lo": lo, "_hi": hi}

        level2 = []
        for qi in range(2):
            for qj in range(2):
                level3 = []
                for si in range(2):
                    for sj in range(2):
                        ci, cj = qi * 2 + si, qj * 2 + sj
                        m = np.zeros(n, bool); m[b:] = True
                        m &= (lx // 250 == ci) & (ly // 250 == cj)
                        if m.sum() > 0:
                            level3.append(node(m, f"{name}_{ci}{cj}.pnts", 0.0, []))
                m = np.zeros(n, bool); m[a:b] = True
                m &= (lx // 500 == qi) & (ly // 500 == qj)
                if m.sum() > 0 or level3:
                    if m.sum() == 0:
                        m[np.flatnonzero((lx // 500 == qi) & (ly // 500 == qj))[:1]] = True
                    level2.append(node(m, f"{name}_q{qi}{qj}.pnts", 2.0, level3))
        m = np.zeros(n, bool); m[:a] = True
        tile_nodes.append(node(m, f"{name}.pnts", 6.0, level2))

    pos = np.concatenate(root_pos); col = np.concatenate(root_rgb); cl = np.concatenate(root_cls)
    lo, hi = write_pnts(out / "root.pnts", pos, col, cl)

    def finish(nd):
        kids = [finish(k) for k in nd.pop("children")]
        lo, hi = nd.pop("_lo"), nd.pop("_hi")
        for k in kids:
            klo, khi = k["_bounds"]
            lo, hi = np.minimum(lo, klo), np.maximum(hi, khi)
        nd["boundingVolume"] = box(lo, hi)
        if kids:
            nd["children"] = [{k2: v for k2, v in k.items() if k2 != "_bounds"} for k in kids]
        nd["_bounds"] = (lo, hi)
        return nd

    kids = [finish(t) for t in tile_nodes]
    for k in kids:
        klo, khi = k["_bounds"]
        lo, hi = np.minimum(lo, klo), np.maximum(hi, khi)

    # Local ENU frame -> ECEF, column-major 4x4 for 3D Tiles.
    r = rot.T
    transform = [r[0, 0], r[1, 0], r[2, 0], 0, r[0, 1], r[1, 1], r[2, 1], 0,
                 r[0, 2], r[1, 2], r[2, 2], 0, origin[0], origin[1], origin[2], 1]
    tileset = {
        "asset": {"version": "1.0", "generator": "MC-3D pipeline/build_tiles.py"},
        "properties": {"classification": {}},
        "geometricError": 60.0,
        "root": {
            "transform": transform,
            "boundingVolume": box(lo, hi),
            "geometricError": 20.0,
            "refine": "ADD",
            "content": {"uri": "root.pnts"},
            "children": [{k2: v for k2, v in k.items() if k2 != "_bounds"} for k in kids],
        },
    }
    with open(out / "tileset.json", "w") as f:
        json.dump(tileset, f, default=float)
    files = len(list(out.glob("*.pnts")))
    size = sum(p.stat().st_size for p in out.glob("*"))
    print(f"total {total:,} points, {files} files, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
