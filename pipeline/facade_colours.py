"""Real façade colours for the buildings that face the circuit, sampled from
Mapillary street photos (CC-BY-SA 4.0).

For each wall that faces the track (the same test as facade_depth() in
build_filmset.py) we look for photos taken 8-60 m in front of it, project a
patch of the wall 4-12 m above the ground into each photo with the photo's
computed camera pose, drop sky, trees and windows, and keep the median
colour. The samples of a building are combined with a median, and the
brightness is clamped to the film set's range so exposure does not show.

The result is a small committed cache, pipeline/facade_colours.json
(BD TOPO cleabs -> colour), that build_filmset.py reads. Run this again only
to refresh it:

  MAPILLARY_TOKEN=... python pipeline/facade_colours.py

Photos are cached in data/raw/filmset/mapillary/ (not committed).
"""

import colorsys
import io
import json
import math
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import shapely
from PIL import Image
from scipy.spatial.transform import Rotation
from shapely.geometry import LineString, shape
from shapely.geometry.polygon import orient
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_filmset as bf  # noqa: E402

OUT_JSON = Path(__file__).resolve().parent / "facade_colours.json"
CACHE = bf.RAW / "mapillary"
BBOX = (7.4163, 43.7312, 7.4360, 43.7455)
FIELDS = ("id,computed_geometry,computed_rotation,camera_type,camera_parameters,"
          "width,height,captured_at,sequence,thumb_1024_url")
V_MIN, V_MAX = 0.72, 0.95          # the film set's wall brightness range
CAM_H = 2.3                        # camera above the ground (car roof, hand held)
MAX_PER_WALL = 14


def api(url):
    for k in range(5):
        try:
            return json.load(urllib.request.urlopen(url, timeout=120))
        except Exception as e:  # network hiccups
            print("retry", e, file=sys.stderr)
            time.sleep(2 + 3 * k)
    return {"data": []}


def list_images(token):
    """All photos in the bbox (the API returns at most 2000 per call, so
    split busy cells)."""
    f = CACHE / "images.json"
    if f.exists():
        return json.load(open(f))
    out = {}

    def rec(b, depth=0):
        u = (f"https://graph.mapillary.com/images?access_token={token}&fields={FIELDS}"
             f"&bbox={b[0]:.5f},{b[1]:.5f},{b[2]:.5f},{b[3]:.5f}&limit=2000")
        d = api(u).get("data", [])
        if len(d) >= 1900 and depth < 6:
            mx, my = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            for bb in [(b[0], b[1], mx, my), (mx, b[1], b[2], my), (b[0], my, mx, b[3]), (mx, my, b[2], b[3])]:
                rec(bb, depth + 1)
        else:
            for i in d:
                out[i["id"]] = i
    x0, y0, x1, y1 = BBOX
    n = 4
    for i in range(n):
        for j in range(n):
            rec((x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * j / n,
                 x0 + (x1 - x0) * (i + 1) / n, y0 + (y1 - y0) * (j + 1) / n))
    CACHE.mkdir(parents=True, exist_ok=True)
    json.dump(list(out.values()), open(f, "w"))
    return list(out.values())


def walls_facing_track(feats, mnt, mns):
    """(cleabs, a, b, outward normal, ground, top) for each wall of 6 m or more
    that faces the circuit within 40 m."""
    tl = bf.Raster(mnt)
    ts = bf.Raster(mns)
    walls, polys = [], []
    extent = shapely.box(bf.X0, bf.Y0, bf.X1, bf.Y1)
    for f in feats:
        p = f["properties"]
        g = shapely.force_2d(shape(f["geometry"])).buffer(0)
        for poly in bf.to_polys(g):
            if poly.area < 10 or not extent.contains(poly.centroid):
                continue
            polys.append(poly)
            if p.get("cleabs") in bf.HEROES or p.get("construction_legere") or p.get("nature") == "Tribune":
                continue
            if bf.TUNNEL is not None and poly.intersects(bf.TUNNEL["bld"]):
                continue                    # over the tunnel: photos there show its ceiling
            if float(bf.track_dist(poly.centroid.x, poly.centroid.y)[0]) > 90:
                continue
            poly = orient(poly.simplify(0.35), 1.0)
            ring = np.asarray(poly.exterior.coords)[:, :2]
            for a, b in zip(ring[:-1], ring[1:]):
                L = float(np.linalg.norm(b - a))
                if L < 6:
                    continue
                u = (b - a) / L
                out = np.array([u[1], -u[0]])
                mid = (a + b) / 2
                d_mid = float(bf.track_dist(*(mid + out * 2.0))[0])
                if d_mid > 40 or float(bf.track_dist(*(mid + out * 8))[0]) > d_mid + 1:
                    continue
                # Where the wall starts: the higher of the ground just outside
                # and just inside it, so a building on a cliff or retaining
                # wall is sampled above the rock, not on it.
                q = np.array([a + (b - a) * t for t in (0.25, 0.5, 0.75)])
                g_out = np.nanmedian(tl.sample(q[:, 0] + out[0] * 1.5, q[:, 1] + out[1] * 1.5))
                g_in = np.nanmedian(tl.sample(q[:, 0] - out[0] * 1.5, q[:, 1] - out[1] * 1.5))
                ground = np.nanmax([g_out, g_in])
                inner = poly.buffer(-1.0)
                if inner.is_empty:
                    inner = poly
                xs, ys = np.meshgrid(np.linspace(*inner.bounds[0::2], 12), np.linspace(*inner.bounds[1::2], 12))
                ins = shapely.contains_xy(inner, xs.ravel(), ys.ravel())
                hs = ts.sample(xs.ravel()[ins], ys.ravel()[ins])
                top = float(np.nanpercentile(hs, 60)) if np.isfinite(hs).any() else ground + 10
                if not np.isfinite(ground) or top - ground < 5:
                    continue
                walls.append((p.get("cleabs"), a, b, out, float(ground), top))
    return walls, polys


TO_L93 = bf.pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)


def local_en(origin, xy):
    """Lambert-93 points -> metres east/north of origin (true north; grid
    north is about 3 degrees off here)."""
    lon0, lat0 = bf.to_lonlat(origin[0], origin[1])
    lon, lat = bf.to_lonlat(xy[:, 0], xy[:, 1])
    k = 111320.0
    return ((np.asarray(lon) - lon0) * k * math.cos(math.radians(lat0)),
            (np.asarray(lat) - lat0) * 110950.0)


def project(img, R, cam_xy, cam_z, pts):
    """L93 points + height -> pixel (u, v) in the 1024 px thumbnail.
    Mapillary's rotation maps local east/north/up to the camera (x right,
    y down, z forward), like OpenSfM."""
    e, n = local_en(cam_xy, pts[:, :2])
    enu = np.column_stack([e, n, pts[:, 2] - cam_z])
    c = enu @ R.T
    z = c[:, 2]
    W, H = img["width"], img["height"]
    s = 1024 / max(W, H)
    w, h = W * s, H * s
    f = img["camera_parameters"][0]
    k1, k2 = (img["camera_parameters"] + [0, 0, 0])[1:3]
    with np.errstate(divide="ignore", invalid="ignore"):
        xn, yn = c[:, 0] / z, c[:, 1] / z
        r2 = xn * xn + yn * yn
        dist = 1 + k1 * r2 + k2 * r2 * r2
        u = f * dist * xn * max(w, h) + w / 2
        v = f * dist * yn * max(w, h) + h / 2
    ok = (z > 1) & (u >= 2) & (u < w - 2) & (v >= 2) & (v < h - 2)
    # The lens polynomial folds back far outside the frame: the undistorted
    # point must be in (or very near) the frame too.
    lim = 0.5 / f * 1.05
    ok &= (np.abs(xn) < lim) & (np.abs(yn) < lim * h / w + 0.02) & (dist > 0.7)
    return u, v, ok, (w, h)


def wall_grid(a, b, out, ground, top):
    """Points on a wall, 4-12 m above the street (lower on short walls),
    leaving the corners out, a few cm in front of it."""
    z1 = min(ground + 12, top - 1.0)
    z0 = ground + 4
    if z1 < z0 + 1.5:
        z0 = ground + 2.5
    T, Z = np.meshgrid(np.linspace(0.08, 0.92, 32), np.linspace(z0, z1, 14))
    P = a + np.outer(T.ravel(), b - a) + out * 0.1
    return np.column_stack([P, Z.ravel()])


def patch_colour(pix):
    """Median wall colour of a patch of pixels, without sky, trees and
    windows (the darkest 40 %)."""
    p = pix.astype(np.float32)
    r, g, b = p[:, 0], p[:, 1], p[:, 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    sky = ((b > r + 12) & (b > g) & (lum > 140)) | (lum > 245)
    tree = (2 * g - r - b) > 15
    keep = ~sky & ~tree
    if keep.sum() < 0.5 * len(p):
        return None
    p, lum = p[keep], lum[keep]
    p = p[lum >= np.percentile(lum, 40)]
    if len(p) < 20:
        return None
    # A wall is fairly even; a patch that caught a street, a palm or the sea is not.
    ch = p / (p.max(axis=1, keepdims=True) + 1)
    if float(np.std(ch, axis=0).max()) > 0.16:
        return None
    return np.median(p, axis=0)


def to_hex(rgb):
    return "#" + "".join(f"{int(round(max(0, min(255, c)))):02x}" for c in rgb)


def main():
    token = os.environ.get("MAPILLARY_TOKEN")
    if not token:
        sys.exit("MAPILLARY_TOKEN is not set")
    mnt, mns = bf.load_raster("mnt.tif"), bf.load_raster("mns.tif")
    bf.TRACK = bf.load_track()
    bf.TUNNEL = bf.load_tunnel()
    feats = json.load(open(bf.RAW / "bdtopo.json"))["features"]
    walls, polys = walls_facing_track(feats, mnt, mns)
    print(len(walls), "walls face the circuit,", len({w[0] for w in walls}), "buildings")
    tree_b = STRtree(polys)

    imgs = [i for i in list_images(token) if i.get("camera_type") == "perspective"
            and i.get("computed_rotation") and i.get("camera_parameters") and i.get("width")
            and i.get("captured_at", 0) > 1.45e12]       # 2016 and later
    print(len(imgs), "perspective photos")
    xy = np.array([TO_L93.transform(*i["computed_geometry"]["coordinates"]) for i in imgs])
    from scipy.spatial import cKDTree
    kd = cKDTree(xy)

    # Which photos see which wall. Most photos look along the street, so a
    # wall is usually seen at an angle and only in part: project a grid over
    # the whole wall and keep the photos that hold enough of it.
    mntR = bf.Raster(mnt)
    cam_z = mntR.sample(xy[:, 0], xy[:, 1])
    plan = []          # (wall index, image index, visible grid points)
    no_photo = set()
    for wi, (cl, a, b, out, ground, top) in enumerate(walls):
        mid = (a + b) / 2
        pts = wall_grid(a, b, out, ground, top)
        cand = []
        for ii in kd.query_ball_point(mid + out * 25, 45):
            d = xy[ii] - mid
            dist = float(np.hypot(*d))
            if not 6 <= dist <= 70 or float(d @ out) < 2:
                continue
            cz = float(cam_z[ii])
            if not np.isfinite(cz) or abs(cz - ground) > 6:
                continue                    # photo on another level (stairs, upper road): pose too unsure
            R = Rotation.from_rotvec(imgs[ii]["computed_rotation"]).as_matrix()
            u, v, ok, _ = project(imgs[ii], R, xy[ii], cz + CAM_H, pts)
            if ok.sum() < 60:
                continue
            c = pts[ok, :2].mean(axis=0)
            dv = xy[ii] - c
            dist = float(np.hypot(*dv))
            cosi = float(dv @ out) / dist
            if cosi < 0.25 or dist > 60:    # too grazing or too far
                continue
            # Nothing else in between.
            seg = LineString([xy[ii], c + out * 0.8])
            if any(polys[j].area > 30 and polys[j].intersection(seg).length > 1.0 for j in tree_b.query(seg)):
                continue
            yrs = (imgs[ii]["captured_at"] / 3.156e10) + 1970
            cand.append((cosi + (yrs - 2016) * 0.06 - dist / 60 + ok.mean() * 0.5, ii, ok))
        cand.sort(key=lambda c: -c[0])
        # Spread over sequences (several passes, several days).
        seen, picked = {}, []
        for sc, ii, ok in cand:
            sq = imgs[ii].get("sequence")
            if seen.get(sq, 0) >= 2:
                continue
            seen[sq] = seen.get(sq, 0) + 1
            picked.append((wi, ii, ok))
            if len(picked) >= MAX_PER_WALL:
                break
        plan += picked
        if not picked:
            no_photo.add(cl)
    print("buildings with a wall no photo sees:", len(no_photo - {walls[w][0] for w, _, _ in plan}))
    need = sorted({ii for _, ii, _ in plan})
    print(len(plan), "wall views from", len(need), "photos")

    CACHE.mkdir(parents=True, exist_ok=True)

    def fetch(ii):
        f = CACHE / f"{imgs[ii]['id']}.jpg"
        if f.exists() and f.stat().st_size > 1000:
            return
        try:
            f.write_bytes(urllib.request.urlopen(imgs[ii]["thumb_1024_url"], timeout=60).read())
        except Exception:
            # Thumbnail links expire: ask for a fresh one.
            u = f"https://graph.mapillary.com/{imgs[ii]['id']}?access_token={token}&fields=thumb_1024_url"
            try:
                url = api(u)["thumb_1024_url"]
                f.write_bytes(urllib.request.urlopen(url, timeout=60).read())
            except Exception as e:
                print("skip", imgs[ii]["id"], e, file=sys.stderr)
    with ThreadPoolExecutor(12) as ex:
        list(ex.map(fetch, need))

    samples = {}
    for wi, ii, vis in plan:
        cl, a, b, out, ground, top = walls[wi]
        f = CACHE / f"{imgs[ii]['id']}.jpg"
        if not f.exists():
            continue
        im = np.asarray(Image.open(f).convert("RGB"))
        if im.mean() < 70:
            continue                    # night or tunnel photo
        R = Rotation.from_rotvec(imgs[ii]["computed_rotation"]).as_matrix()
        u, v, ok, (w, h) = project(imgs[ii], R, xy[ii], float(cam_z[ii]) + CAM_H, wall_grid(a, b, out, ground, top))
        if im.shape[1] < w - 4 or np.median(v[ok]) > 0.55 * h:
            continue                    # a wall 4-12 m up sits above the horizon; this pose is off
        sx, sy = im.shape[1] / w, im.shape[0] / h
        pix = im[(v[ok] * sy).astype(int).clip(0, im.shape[0] - 1), (u[ok] * sx).astype(int).clip(0, im.shape[1] - 1)]
        c = patch_colour(pix)
        if c is not None:
            samples.setdefault(cl, []).append((c, imgs[ii]["id"], imgs[ii].get("sequence")))

    result = {}
    print("buildings with samples:", len(samples), "with 3+:", sum(len(v) >= 3 for v in samples.values()))
    for cl, ss in samples.items():
        if len(ss) < 4 or len({s[2] for s in ss}) < 2:
            continue
        cols = np.array([s[0] for s in ss])
        # Keep the samples that agree: a few photos still miss the wall.
        ch = cols / cols.max(axis=1, keepdims=True)
        dev = np.abs(ch - np.median(ch, axis=0)).sum(axis=1)
        good = dev <= max(0.08, np.median(dev) * 2)
        if good.sum() < 4:
            continue
        cols = cols[good]
        ss = [s for s, g in zip(ss, good) if g]
        v = cols.max(axis=1)
        # Hue and saturation from the sunlit half (shade turns walls blue).
        lit = v >= np.median(v)
        chroma = np.median(cols[lit] / v[lit, None], axis=0)
        h, s, _ = colorsys.rgb_to_hsv(*(chroma / chroma.max()))
        # Brightness: the photo's, mapped into the film set's range.
        V = float(np.interp(np.median(v[lit]) / 255, [0.35, 0.9], [V_MIN, V_MAX]))
        # Walls here are not blue or green: those come from glass, sky and
        # trees left in the patch, so keep only a hint of them.
        # Street photos wash colour out (haze, overexposed walls): the pink
        # and terracotta fronts came out greyish next to the photos, so
        # saturation is lifted by a third.
        s = min(s * 1.35, 0.06 if 0.2 < h < 0.8 else 0.42)
        rgb = np.array(colorsys.hsv_to_rgb(h, s, V)) * 255
        result[cl] = {"colour": to_hex(rgb), "photos": len(ss)}
    print(len(result), "buildings coloured of", len({w[0] for w in walls}))
    OUT_JSON.write_text(json.dumps({
        "source": "Mapillary contributors (CC-BY-SA 4.0)",
        "method": "median wall colour 4-12 m up, sky/trees/windows dropped; brightness mapped to V 0.72-0.95",
        "buildings": dict(sorted(result.items())),
    }, indent=1))


if __name__ == "__main__":
    main()
