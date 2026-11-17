#!/usr/bin/env python3
"""Build 3D buildings with straight walls from IGN building outlines + LiDAR.

For every building outline (IGN BD TOPO, which also covers Monaco):
  - the roof is the outline triangulated into ~2 m triangles whose heights
    come from the LiDAR surface model (MNS, 50 cm), so domes, slopes and
    terraces keep their real shape;
  - the walls are vertical, from the bare ground (LiDAR terrain model, MNT)
    up to the roof edge, so building fronts are crisp instead of melted.
Roofs are textured with the IGN 25 cm orthophoto; walls get a Riviera
render colour per building, darker towards the street.

Output: build/buildings/tileset.json + one .glb per 1 km tile (3D Tiles 1.1),
in the same local frame as the point cloud.

Usage:
  python pipeline/build_buildings.py --out build/buildings
"""
import argparse
import glob
import hashlib
import json
import struct
import sys
from pathlib import Path

import numpy as np
import pyproj
import triangle
from shapely.geometry import shape, Polygon, MultiPolygon
from shapely.geometry.polygon import orient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_surface import load_mosaic  # noqa: E402
from build_tiles import enu_frame, CENTER_LON, CENTER_LAT  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
MAX_TRI_AREA = 6.0      # m², roof triangle size
MIN_AREA = 6.0          # m², skip sheds and slivers
MIN_HEIGHT = 2.5        # m above ground

# Riviera façade colours (render and stone), picked per building.
FACADE = np.array([
    [236, 226, 204], [232, 210, 170], [226, 190, 160], [240, 233, 220],
    [222, 200, 150], [214, 178, 150], [228, 220, 205], [205, 196, 182],
], np.float32)


def filt(a, fn):
    """3 x 3 max or min filter that ignores NaN."""
    fill = -np.inf if fn is np.fmax else np.inf
    b = np.where(np.isnan(a), fill, a)
    out = b.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            out = fn(out, np.roll(np.roll(b, dy, 0), dx, 1))
    out[~np.isfinite(out)] = np.nan
    return out


class Raster:
    def __init__(self, arr, x0, y0, res=0.5):
        self.a, self.x0, self.y0, self.res = arr, x0, y0, res

    def sample(self, x, y):
        c = np.round((x - self.x0) / self.res).astype(int)
        r = np.round((self.y0 - y) / self.res).astype(int)
        ok = (c >= 0) & (c < self.a.shape[1]) & (r >= 0) & (r < self.a.shape[0])
        out = np.full(len(x), np.nan, np.float32)
        out[ok] = self.a[r[ok], c[ok]]
        return out


def interior_heights(r, poly):
    """Surface-model heights at the raster cells inside a polygon."""
    from shapely import contains_xy
    x0, y0, x1, y1 = poly.bounds
    xs = np.arange(np.floor(x0 / r.res) * r.res + r.res / 2, x1, r.res)
    ys = np.arange(np.floor(y0 / r.res) * r.res + r.res / 2, y1, r.res)
    if not len(xs) or not len(ys):
        return np.array([])
    X, Y = np.meshgrid(xs, ys)
    X, Y = X.ravel(), Y.ravel()
    if len(X) > 40000:
        k = np.random.default_rng(0).choice(len(X), 40000, replace=False)
        X, Y = X[k], Y[k]
    ins = contains_xy(poly, X, Y)
    z = r.sample(X[ins], Y[ins])
    return z[np.isfinite(z)]


def facade_texture():
    """One window bay by one storey, repeated over every wall. White render
    (the vertex colour tints it), a tall window with a stone surround and
    green Riviera shutters folded open either side, and a floor band."""
    from PIL import Image, ImageDraw, ImageFilter
    import io
    W = H = 128
    img = Image.new("RGB", (W, H), (246, 244, 240))
    d = ImageDraw.Draw(img)
    rng = np.random.default_rng(1)
    px = np.asarray(img).astype(np.int16)
    px = (px + rng.integers(-6, 7, (H, W, 1))).clip(0, 255).astype(np.uint8)
    img = Image.fromarray(px)
    d = ImageDraw.Draw(img)
    d.rectangle([0, H - 9, W, H - 1], fill=(222, 216, 204))          # floor band
    d.rectangle([0, H - 10, W, H - 9], fill=(200, 194, 182))
    wx0, wx1, wy0, wy1 = 48, 80, 26, 100                             # window
    d.rectangle([wx0 - 5, wy0 - 5, wx1 + 5, wy1 + 4], fill=(226, 220, 208))   # surround
    d.rectangle([wx0, wy0, wx1, wy1], fill=(70, 80, 92))                       # glass
    d.rectangle([wx0, wy0, wx1, wy0 + 30], fill=(112, 128, 142))               # sky reflection
    d.line([(64, wy0), (64, wy1)], fill=(214, 210, 200), width=2)              # mullion
    d.line([(wx0, 52), (wx1, 52)], fill=(214, 210, 200), width=2)
    for sx in (wx0 - 19, wx1 + 6):                                             # shutters
        d.rectangle([sx, wy0 - 2, sx + 13, wy1 + 2], fill=(150, 160, 140))
        for yy in range(wy0 + 2, wy1, 5):
            d.line([(sx + 2, yy), (sx + 11, yy)], fill=(128, 138, 120), width=1)
    d.rectangle([wx0 - 7, wy1 + 4, wx1 + 7, wy1 + 8], fill=(206, 198, 184))   # sill
    img = img.filter(ImageFilter.SMOOTH)
    b = io.BytesIO()
    img.save(b, "JPEG", quality=88)
    return b.getvalue()


ROOF_MARGIN = 200   # m of neighbouring photo around each km tile
ROOF_TEX = 4096     # px; a safe texture size on phones


def roof_texture(tx, ty):
    """The tile's orthophoto plus a margin from its neighbours, so buildings
    that cross a tile edge get the right roof photo instead of smeared edge
    pixels. Returns jpeg bytes covering [tx*1000 - M, (tx+1)*1000 + M] x
    [(ty-1)*1000 - M, ty*1000 + M]."""
    from PIL import Image
    import io
    px = 4                                    # source px per metre
    span = 1000 + 2 * ROOF_MARGIN
    canvas = Image.new("RGB", (span * px, span * px), (90, 90, 90))
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            f = RAW / "ortho" / f"ortho_{tx + dx}_{ty + dy}.jpg"
            if not f.exists():
                continue
            im = Image.open(f)
            # Top-left of that tile on the canvas.
            ox = (dx * 1000 + ROOF_MARGIN) * px
            oy = (-dy * 1000 + ROOF_MARGIN) * px
            canvas.paste(im, (ox, oy))
    canvas = canvas.resize((ROOF_TEX, ROOF_TEX), Image.LANCZOS)
    b = io.BytesIO()
    canvas.save(b, "JPEG", quality=85)
    return b.getvalue()


def polygons(geom):
    g = shape(geom)
    g = g.buffer(0)
    parts = g.geoms if isinstance(g, MultiPolygon) else [g]
    return [p for p in parts if isinstance(p, Polygon) and p.area >= MIN_AREA]


def triangulate(poly):
    # Outer ring counter-clockwise, courtyards clockwise: walls built along the
    # rings then face outward.
    poly = orient(poly.simplify(0.15), 1.0)
    if poly.is_empty or not isinstance(poly, Polygon):
        return None
    rings = [poly.exterior] + list(poly.interiors)
    verts, segs, holes = [], [], []
    for k, ring in enumerate(rings):
        pts = np.asarray(ring.coords)[:-1, :2]
        if len(pts) < 3:
            return None
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
    try:
        # Y: no extra points on the outline, so roof edges and wall tops match.
        t = triangle.triangulate(data, f"pYq20a{MAX_TRI_AREA}")
    except Exception:
        return None
    if "triangles" not in t or len(t["vertices"]) < len(verts):
        return None
    # Input vertices keep their order, so data["segments"] still index the
    # oriented rings.
    return t["vertices"], t["triangles"], data["segments"]


def write_glb(path, roof, wall, texture_bytes, facade_bytes):
    """roof/wall: dicts of numpy arrays. glTF is y-up: we write (e, u, -n)."""
    def yup(p):
        return np.column_stack([p[:, 0], p[:, 2], -p[:, 1]]).astype("<f4")

    chunks, views, accessors = [], [], []

    def add(arr, target=None, comp=5126, typ="VEC3", normalized=False, minmax=False):
        b = arr.tobytes()
        off = sum(len(c) for c in chunks)
        chunks.append(b + b"\0" * ((-len(b)) % 4))
        view = {"buffer": 0, "byteOffset": off, "byteLength": len(b)}
        if target:
            view["target"] = target
        views.append(view)
        acc = {"bufferView": len(views) - 1, "componentType": comp, "count": len(arr), "type": typ}
        if normalized:
            acc["normalized"] = True
        if minmax:
            acc["min"] = arr.min(axis=0).tolist(); acc["max"] = arr.max(axis=0).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    prims = []
    if len(roof["idx"]):
        a_p = add(yup(roof["pos"]), 34962, minmax=True)
        a_n = add(yup(roof["nrm"]), 34962)
        a_t = add(roof["uv"].astype("<f4"), 34962, typ="VEC2")
        a_i = add(roof["idx"].astype("<u4"), 34963, comp=5125, typ="SCALAR")
        prims.append({"attributes": {"POSITION": a_p, "NORMAL": a_n, "TEXCOORD_0": a_t}, "indices": a_i, "material": 0})
    if len(wall["pos"]):
        a_p = add(yup(wall["pos"]), 34962, minmax=True)
        a_n = add(yup(wall["nrm"]), 34962)
        a_c = add(wall["col"].astype("u1"), 34962, comp=5121, typ="VEC4", normalized=True)
        a_t = add(wall["uv"].astype("<f4"), 34962, typ="VEC2")
        prims.append({"attributes": {"POSITION": a_p, "NORMAL": a_n, "COLOR_0": a_c, "TEXCOORD_0": a_t}, "material": 1})
    img_views = []
    for data in (texture_bytes, facade_bytes):
        img_off = sum(len(c) for c in chunks)
        chunks.append(data + b"\0" * ((-len(data)) % 4))
        views.append({"buffer": 0, "byteOffset": img_off, "byteLength": len(data)})
        img_views.append(len(views) - 1)
    binary = b"".join(chunks)
    gltf = {
        "asset": {"version": "2.0", "generator": "MC-3D build_buildings.py"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": prims}],
        "materials": [
            {"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "metallicFactor": 0, "roughnessFactor": 0.9}},
            {"pbrMetallicRoughness": {"baseColorTexture": {"index": 1}, "metallicFactor": 0, "roughnessFactor": 0.85}},
        ],
        "textures": [{"source": 0, "sampler": 0}, {"source": 1, "sampler": 1}],
        "samplers": [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071},
                     {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}],
        "images": [{"bufferView": img_views[0], "mimeType": "image/jpeg"},
                   {"bufferView": img_views[1], "mimeType": "image/jpeg"}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": views, "accessors": accessors,
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    total = 12 + 8 + len(js) + 8 + len(binary)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        f.write(struct.pack("<II", len(binary), 0x004E4942) + binary)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "build" / "buildings"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    names = [l.strip() for l in open(ROOT / "pipeline" / "tiles.txt") if l.strip() and not l.startswith("#")]
    mns, x0, y0 = load_mosaic(names, "mns")
    mnt, _, _ = load_mosaic(names, "mnt")
    roof_r = Raster(filt(mns, np.fmax), x0, y0)
    mns_r = Raster(mns, x0, y0)   # max filter: roof edges do not drop to the street
    ground_r = Raster(filt(mnt, np.fmin), x0, y0)

    pyproj.datadir.append_data_dir(str(RAW / "grids"))
    g = pyproj.transformer.TransformerGroup("EPSG:2154+5720", "EPSG:4979", always_xy=True)
    to_geo = next(t for t in g.transformers if "NGF-IGN69 height" in t.description and "ballpark" not in t.description)
    to_ecef = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    rot, origin = enu_frame(CENTER_LON, CENTER_LAT)

    def to_enu(x, y, z):
        lon, lat, h = to_geo.transform(x, y, z)
        ex, ey, ez = to_ecef.transform(lon, lat, h)
        return (np.column_stack([ex, ey, ez]) - origin) @ rot.T

    feats = []
    for f in sorted(glob.glob(str(RAW / "buildings" / "*.json"))):
        feats.extend(json.load(open(f))["features"])
    print(len(feats), "outlines")

    tiles = {n: {"roof": [], "wall": []} for n in names}
    facade = facade_texture()
    kept = 0
    for feat in feats:
        props = feat["properties"]
        for poly in polygons(feat["geometry"]):
            c = poly.centroid
            name = f"{int(c.x // 1000)}_{int(c.y // 1000) + 1}"
            if name not in tiles:
                continue
            tri = triangulate(poly)
            if tri is None:
                continue
            v, t, segs = tri
            zr = roof_r.sample(v[:, 0], v[:, 1])
            # Outline corners can pick up a taller neighbour, a tree or a
            # crane in the max-filtered model, which made shards and spikes.
            # Keep every roof vertex within the range of the unfiltered
            # surface model inside the outline (eroded by 1 m).
            inner = poly.buffer(-1.0)
            if inner.is_empty:
                inner = poly
            zin = interior_heights(mns_r, inner)
            if len(zin) >= 4:
                lo_r, hi_r = np.percentile(zin, [10, 97])
                zr = np.clip(zr, lo_r, hi_r)
            zg = ground_r.sample(v[:, 0], v[:, 1])
            if np.isnan(zg).all():
                gmin = props.get("altitude_minimale_sol")
                if gmin is None:
                    continue
                zg[:] = gmin
            zg = np.where(np.isnan(zg), np.nanmin(zg), zg)
            # On cliff edges (Le Rocher) an outline corner can sample the
            # ground far below; keep walls within 8 m of the building's
            # typical ground level so they do not hang down the cliff.
            zg = np.maximum(zg, np.percentile(zg, 75) - 8.0)
            if np.isnan(zr).all():
                h = props.get("hauteur") or 6.0
                zr[:] = np.nanmax(zg) + h
            zr = np.where(np.isnan(zr), np.nanmedian(zr), zr)
            # A roof is never below the ground next to it.
            zr = np.maximum(zr, zg + MIN_HEIGHT)
            if np.median(zr - zg) < MIN_HEIGHT:
                continue
            seed = int(hashlib.md5(str(props.get("cleabs", kept)).encode()).hexdigest()[:8], 16)
            tiles[name]["roof"].append((v, t, zr))
            bay = 3.6 + (seed >> 8) % 9 * 0.15          # 3.6 to 4.8 m
            storey = 2.9 + (seed >> 12) % 5 * 0.12      # 2.9 to 3.4 m
            tiles[name]["wall"].append((v, segs, zr, zg, FACADE[seed % len(FACADE)], bay, storey))
            kept += 1
    print(kept, "buildings kept")

    children = []
    for name, parts in tiles.items():
        if not parts["roof"]:
            continue
        tx, ty = (int(s) for s in name.split("_"))
        # Roofs: shared vertices, smooth normals, UV into this tile's photo.
        pos, idx, uv = [], [], []
        base = 0
        for v, t, zr in parts["roof"]:
            pos.append(to_enu(v[:, 0], v[:, 1], zr))
            span = 1000 + 2 * ROOF_MARGIN
            uv.append(np.column_stack([(v[:, 0] - tx * 1000 + ROOF_MARGIN) / span,
                                       (ty * 1000 + ROOF_MARGIN - v[:, 1]) / span]))
            idx.append(t + base)
            base += len(v)
        rp = np.concatenate(pos); ri = np.concatenate(idx); ruv = np.clip(np.concatenate(uv), 0, 1)
        # Fix winding so roofs face up, then accumulate vertex normals.
        a, b, c = rp[ri[:, 0]], rp[ri[:, 1]], rp[ri[:, 2]]
        fn = np.cross(b - a, c - a)
        flip = fn[:, 2] < 0
        ri[flip] = ri[flip][:, [0, 2, 1]]
        fn[flip] *= -1
        rn = np.zeros_like(rp)
        for k in range(3):
            np.add.at(rn, ri[:, k], fn)
        rn /= np.linalg.norm(rn, axis=1, keepdims=True) + 1e-9

        # Walls: one quad per outline segment, flat normals, vertex colours.
        wp, wc, wuv = [], [], []
        for v, segs, zr, zg, col, bay, storey in parts["wall"]:
            i, j = segs[:, 0], segs[:, 1]
            # u: metres along the outline / bay width; v: height above the
            # building's ground / storey height, so windows line up floor by
            # floor around the building.
            seg_len = np.hypot(*(v[j] - v[i]).T)
            u0 = np.concatenate([[0], np.cumsum(seg_len)[:-1]]) / bay
            u1 = u0 + seg_len / bay
            base_z = np.min(zg)
            vg_i, vg_j = (zg[i] - base_z) / storey, (zg[j] - base_z) / storey
            vr_i, vr_j = (zr[i] - base_z) / storey, (zr[j] - base_z) / storey
            uvq = np.stack([np.column_stack([u0, vg_i]), np.column_stack([u1, vg_j]),
                            np.column_stack([u1, vr_j]), np.column_stack([u0, vr_i])], axis=1)
            wuv.append(np.concatenate([uvq[:, [0, 1, 2]], uvq[:, [0, 2, 3]]], axis=0))
            p = np.stack([
                to_enu(v[i, 0], v[i, 1], zg[i]), to_enu(v[j, 0], v[j, 1], zg[j]),
                to_enu(v[j, 0], v[j, 1], zr[j]), to_enu(v[i, 0], v[i, 1], zr[i]),
            ], axis=1)                                          # (nseg, 4, 3)
            quads = np.concatenate([p[:, [0, 1, 2]], p[:, [0, 2, 3]]], axis=0)
            # Darker at street level, a simple stand-in for ambient occlusion.
            shade = np.concatenate([np.tile([0.72, 0.72, 1.0], (len(p), 1)), np.tile([0.72, 1.0, 1.0], (len(p), 1))])
            colours = (col[None, None, :] * shade[:, :, None]).clip(0, 255)
            wp.append(quads); wc.append(colours)
        wp = np.concatenate(wp)                                  # (ntri, 3, 3)
        wuv = np.concatenate(wuv)
        wc = np.concatenate(wc)
        n = np.cross(wp[:, 1] - wp[:, 0], wp[:, 2] - wp[:, 0])
        n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-9
        # Rings are oriented, so (ground_i, ground_j, roof_j) faces outward.
        wp2, n2, wc2 = wp, n, wc
        wall = {
            "pos": wp2.reshape(-1, 3),
            "nrm": np.repeat(n2, 3, axis=0),
            "col": np.concatenate([wc2.reshape(-1, 3), np.full((len(wc2) * 3, 1), 255)], axis=1),
            # glTF v runs down the image: flip so storeys stack upward.
            "uv": np.column_stack([wuv.reshape(-1, 2)[:, 0], -wuv.reshape(-1, 2)[:, 1]]),
        }
        tex = roof_texture(tx, ty)
        write_glb(out / f"{name}.glb", {"pos": rp, "nrm": rn, "uv": ruv, "idx": ri}, wall, tex, facade)
        allp = np.concatenate([rp, wall["pos"]])
        lo, hi = allp.min(axis=0), allp.max(axis=0)
        cen, half = (lo + hi) / 2, (hi - lo) / 2 + 1
        children.append({
            "boundingVolume": {"box": [*cen, half[0], 0, 0, 0, half[1], 0, 0, 0, half[2]]},
            "geometricError": 0, "content": {"uri": f"{name}.glb"},
        })
        print(name, len(parts["roof"]), "buildings", round((out / f"{name}.glb").stat().st_size / 1e6, 1), "MB")

    boxes = np.array([c["boundingVolume"]["box"] for c in children])
    lo = (boxes[:, :3] - boxes[:, [3, 7, 11]]).min(axis=0)
    hi = (boxes[:, :3] + boxes[:, [3, 7, 11]]).max(axis=0)
    cen, half = (lo + hi) / 2, (hi - lo) / 2
    r = rot.T
    transform = [r[0, 0], r[1, 0], r[2, 0], 0, r[0, 1], r[1, 1], r[2, 1], 0,
                 r[0, 2], r[1, 2], r[2, 2], 0, origin[0], origin[1], origin[2], 1]
    tileset = {
        "asset": {"version": "1.1", "generator": "MC-3D pipeline/build_buildings.py"},
        "geometricError": 500,
        "root": {"transform": transform, "refine": "ADD", "geometricError": 150,
                 "boundingVolume": {"box": [*cen, half[0], 0, 0, 0, half[1], 0, 0, 0, half[2]]},
                 "children": children},
    }
    with open(out / "tileset.json", "w") as f:
        json.dump(tileset, f, default=float)
    print("done")


if __name__ == "__main__":
    main()
