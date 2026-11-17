"""Package a Gaussian splat as a geo-placed 3D Tiles tileset for CesiumJS.

Input : a .spz file (SPZ v2/v3). Make one from any .ply/.splat/.sog with
        PlayCanvas splat-transform, e.g.
          splat-transform scan.ply -S 0,0,0,40 scan.spz --spz-version 3
Output: <out>/tileset.json + <out>/splat.glb  (glTF KHR_gaussian_splatting
        with KHR_gaussian_splatting_compression_spz_2, what CesiumJS >= 1.139
        reads).

The splat's own frame is taken as glTF (Y up, metres). It is placed with an
East-North-Up frame at --lon/--lat/--height (ellipsoid metres), turned by
--heading degrees (clockwise from north) and scaled by --scale.

Usage:
  python3 pipeline/build_splat.py scan.spz viewer/splats/casino \
      --lon 7.4282 --lat 43.7393 --height 70 --heading 0 --scale 1
"""
import argparse, gzip, json, math, os, struct


def spz_header(path):
    with gzip.open(path) as f:
        magic, version, count, sh, frac, flags, _ = struct.unpack("<IIIBBBB", f.read(16))
    if magic != 0x5053474E:
        raise SystemExit(f"{path} is not an SPZ file")
    if version not in (2, 3):
        raise SystemExit(f"SPZ v{version}: write v3 (--spz-version 3), CesiumJS reads v2/v3")
    return count, sh


def spz_positions_bounds(path):
    """Decode positions (24-bit fixed point) to get min/max for the accessor."""
    with gzip.open(path) as f:
        data = f.read()
    _, _, n, _, frac, _, _ = struct.unpack("<IIIBBBB", data[:16])
    raw = data[16:16 + n * 9]
    s = 1.0 / (1 << frac)
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for i in range(0, n * 9, 3):
        v = raw[i] | raw[i + 1] << 8 | raw[i + 2] << 16
        if v & 0x800000:
            v -= 1 << 24
        k = (i // 3) % 3
        x = v * s
        if x < lo[k]: lo[k] = x
        if x > hi[k]: hi[k] = x
    return lo, hi


def enu_matrix(lon, lat, h, heading, scale):
    """Column-major 4x4: ENU at (lon,lat,h) * rotZ(-heading) * scale."""
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    lo, la = math.radians(lon), math.radians(lat)
    N = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
    X = (N + h) * math.cos(la) * math.cos(lo)
    Y = (N + h) * math.cos(la) * math.sin(lo)
    Z = (N * (1 - e2) + h) * math.sin(la)
    east = [-math.sin(lo), math.cos(lo), 0]
    north = [-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)]
    up = [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)]
    t = math.radians(heading)
    # local x axis points to heading+90 (east when heading 0), local y to heading
    ex = [scale * (math.cos(t) * east[i] - math.sin(t) * north[i]) for i in range(3)]
    ey = [scale * (math.sin(t) * east[i] + math.cos(t) * north[i]) for i in range(3)]
    ez = [scale * up[i] for i in range(3)]
    return ex + [0] + ey + [0] + ez + [0] + [X, Y, Z, 1]


def build_glb(spz_path, out_glb):
    count, sh = spz_header(spz_path)
    lo, hi = spz_positions_bounds(spz_path)
    blob = open(spz_path, "rb").read()
    pad = (-len(blob)) % 4
    blob += b"\0" * pad

    def acc(t, ctype, norm=False, mm=None):
        a = {"count": count, "type": t, "componentType": ctype}
        if norm: a["normalized"] = True
        if mm: a["min"], a["max"] = mm
        return a

    accessors = [
        acc("VEC3", 5126, mm=(lo, hi)),          # POSITION
        acc("VEC4", 5121, norm=True),            # COLOR_0
        acc("VEC3", 5126),                       # SCALE
        acc("VEC4", 5126),                       # ROTATION
    ]
    attrs = {"POSITION": 0, "COLOR_0": 1,
             "KHR_gaussian_splatting:SCALE": 2, "KHR_gaussian_splatting:ROTATION": 3}
    for l in range(1, sh + 1):
        for n in range(2 * l + 1):
            attrs[f"KHR_gaussian_splatting:SH_DEGREE_{l}_COEF_{n}"] = len(accessors)
            accessors.append(acc("VEC3", 5126))

    gltf = {
        "asset": {"version": "2.0", "generator": "MC-3D build_splat.py"},
        "extensionsUsed": ["KHR_gaussian_splatting", "KHR_gaussian_splatting_compression_spz_2"],
        "extensionsRequired": ["KHR_gaussian_splatting", "KHR_gaussian_splatting_compression_spz_2"],
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(blob) - pad}],
        "accessors": accessors,
        "meshes": [{"primitives": [{
            "mode": 0,
            "attributes": attrs,
            "extensions": {"KHR_gaussian_splatting": {
                "kernel": "ellipse", "colorSpace": "srgb_rec709_display",
                "extensions": {"KHR_gaussian_splatting_compression_spz_2": {"bufferView": 0}}}},
        }]}],
        "nodes": [{"mesh": 0}],
        "scenes": [{"nodes": [0]}],
        "scene": 0,
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    with open(out_glb, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A)); f.write(js)
        f.write(struct.pack("<II", len(blob), 0x004E4942)); f.write(blob)
    return count, lo, hi


def main():
    p = argparse.ArgumentParser()
    p.add_argument("spz"); p.add_argument("out")
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--height", type=float, required=True, help="ellipsoid height of the splat origin, m")
    p.add_argument("--heading", type=float, default=0)
    p.add_argument("--scale", type=float, default=1)
    a = p.parse_args()
    os.makedirs(a.out, exist_ok=True)
    n, lo, hi = build_glb(a.spz, os.path.join(a.out, "splat.glb"))
    # glTF Y-up -> tile Z-up: (x, y, z) -> (x, -z, y)
    c = [(lo[i] + hi[i]) / 2 for i in range(3)]
    r = math.dist(lo, hi) / 2 * a.scale
    tileset = {
        "asset": {"version": "1.1"},
        "extensionsUsed": ["3DTILES_content_gltf"],
        "extensions": {"3DTILES_content_gltf": {
            "extensionsUsed": ["KHR_gaussian_splatting", "KHR_gaussian_splatting_compression_spz_2"],
            "extensionsRequired": ["KHR_gaussian_splatting", "KHR_gaussian_splatting_compression_spz_2"]}},
        "geometricError": 50,
        "root": {
            "transform": enu_matrix(a.lon, a.lat, a.height, a.heading, a.scale),
            # in the tile's Z-up frame, before the transform (which scales)
            "boundingVolume": {"sphere": [c[0], -c[2], c[1], r / a.scale]},
            "geometricError": 0, "refine": "ADD",
            "content": {"uri": "splat.glb"},
        },
    }
    json.dump(tileset, open(os.path.join(a.out, "tileset.json"), "w"), indent=1)
    print(f"{n} splats, {os.path.getsize(os.path.join(a.out, 'splat.glb'))/1e6:.1f} MB -> {a.out}")


if __name__ == "__main__":
    main()
