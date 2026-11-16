#!/usr/bin/env python3
"""Turn the IGN LiDAR HD surface model (MNS, 50 cm) into Cesium height tiles.

The viewer drapes the IGN 20 cm orthophoto over these tiles, which gives a
solid, photo-textured city (roofs, streets, port, hills) instead of points.

Output: build/surface/<level>/<x>/<y>.bin in Cesium's geographic tiling
scheme, levels 13 to 19, 65 x 65 samples per tile, plus index.json.
Each sample is uint16: round((ellipsoid height + 100 m) * 50), 0 = no data
(the viewer then falls back to the global terrain).

Usage:
  python pipeline/build_surface.py --out build/surface
"""
import argparse
import json
from pathlib import Path

import numpy as np
import pyproj
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
SIZE = 65
MIN_LEVEL, MAX_LEVEL = 13, 19
NODATA = -9999.0


def load_mosaic(names):
    xs = [int(n.split("_")[0]) for n in names]
    ys = [int(n.split("_")[1]) for n in names]
    x0, y0 = min(xs) * 1000, max(ys) * 1000          # top-left, pixel centre
    w = (max(xs) - min(xs) + 1) * 2000
    h = (max(ys) - min(ys) + 1) * 2000
    mos = np.full((h, w), np.nan, np.float32)
    for n, tx, ty in zip(names, xs, ys):
        a = np.asarray(Image.open(RAW / "mns" / f"mns_{n}.tif"), dtype=np.float32).copy()
        a[a <= NODATA + 1] = np.nan
        r = (y0 - ty * 1000) * 2
        c = (tx * 1000 - x0) * 2
        mos[r:r + 2000, c:c + 2000] = a
    return mos, x0, y0


def pyramid(mos, levels):
    """Mean-downsampled copies so coarse tiles do not alias."""
    out = [mos]
    for _ in range(levels):
        a = out[-1]
        h, w = (a.shape[0] // 2) * 2, (a.shape[1] // 2) * 2
        b = a[:h, :w].reshape(h // 2, 2, w // 2, 2)
        with np.errstate(invalid="ignore"):
            out.append(np.nanmean(b, axis=(1, 3)).astype(np.float32))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "build" / "surface"))
    args = ap.parse_args()
    out = Path(args.out)

    names = [l.strip() for l in open(ROOT / "pipeline" / "tiles.txt") if l.strip() and not l.startswith("#")]
    mos, x0, y0 = load_mosaic(names)
    pyr = pyramid(mos, 6)
    print("mosaic", mos.shape, "valid", round(float(np.isfinite(mos).mean()), 3))

    to_l93 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    to_wgs = pyproj.Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    # Geoid height (ellipsoid minus IGN69) from the RAF20 grid, at the corners
    # of the area; it varies by only a few centimetres over 4 km.
    pyproj.datadir.append_data_dir(str(RAW / "grids"))
    g = pyproj.transformer.TransformerGroup("EPSG:2154+5720", "EPSG:4979", always_xy=True)
    vt = next(t for t in g.transformers if "NGF-IGN69 height" in t.description and "ballpark" not in t.description)
    h_px, w_px = mos.shape
    cx = np.array([x0, x0 + w_px / 2, x0, x0 + w_px / 2])
    cy = np.array([y0, y0, y0 - h_px / 2, y0 - h_px / 2])
    n_corners = np.array(vt.transform(cx, cy, np.zeros(4))[2])
    print("geoid N at corners", n_corners.round(3))

    lon_c, lat_c = to_wgs.transform(cx, cy)
    west, east = max(lon_c[0], lon_c[2]), min(lon_c[1], lon_c[3])
    south, north = max(lat_c[2], lat_c[3]), min(lat_c[0], lat_c[1])

    index = {"minLevel": MIN_LEVEL, "maxLevel": MAX_LEVEL, "encoding": "uint16 (h+100)*50, 0=nodata", "levels": {}}
    count = 0
    for level in range(MIN_LEVEL, MAX_LEVEL + 1):
        step = 180.0 / 2 ** level
        tx0, tx1 = int((west + 180) // step), int((east + 180) // step)
        ty0, ty1 = int((90 - north) // step), int((90 - south) // step)
        index["levels"][level] = [tx0, tx1, ty0, ty1]
        # Sample spacing in metres -> pyramid level.
        spacing = step / (SIZE - 1) * 111_000 * 0.72
        p = int(np.clip(np.floor(np.log2(max(spacing / 0.5, 1))), 0, len(pyr) - 1))
        grid, res = pyr[p], 0.5 * 2 ** p
        u = np.linspace(0, 1, SIZE)
        for tx in range(tx0, tx1 + 1):
            for ty in range(ty0, ty1 + 1):
                lon = -180 + (tx + u) * step
                lat = 90 - (ty + u) * step
                LON, LAT = np.meshgrid(lon, lat)
                X, Y = to_l93.transform(LON.ravel(), LAT.ravel())
                X, Y = np.asarray(X), np.asarray(Y)
                ci = np.round((X - x0) / res).astype(int)
                ri = np.round((y0 - Y) / res).astype(int)
                ok = (ci >= 0) & (ci < grid.shape[1]) & (ri >= 0) & (ri < grid.shape[0])
                hgt = np.full(X.shape, np.nan, np.float32)
                hgt[ok] = grid[ri[ok], ci[ok]]
                # Bilinear geoid between the area's corners.
                fx = np.clip((X - x0) / (w_px / 2), 0, 1)
                fy = np.clip((y0 - Y) / (h_px / 2), 0, 1)
                n_geo = (n_corners[0] * (1 - fx) * (1 - fy) + n_corners[1] * fx * (1 - fy)
                         + n_corners[2] * (1 - fx) * fy + n_corners[3] * fx * fy)
                enc = np.where(np.isfinite(hgt), np.round((hgt + n_geo + 100) * 50), 0)
                if not (enc > 0).any():
                    continue
                d = out / str(level) / str(tx)
                d.mkdir(parents=True, exist_ok=True)
                enc.clip(0, 65535).astype("<u2").tofile(d / f"{ty}.bin")
                count += 1
        print(f"level {level}: tiles x {tx0}-{tx1}, y {ty0}-{ty1}")

    with open(out / "index.json", "w") as f:
        json.dump(index, f)
    size = sum(p.stat().st_size for p in out.rglob("*.bin"))
    print(f"{count} tiles, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
