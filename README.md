# MC-3D

Hyper-realistic, geographically exact 3D Monaco, built from open data.
You start from a view of the Riviera and zoom down into the streets of Monaco.

The first prototype uses the **IGN LiDAR HD** airborne laser scan (the same kind
of data behind the Paris LiDAR visualisations), coloured with IGN orthophotos and
streamed as OGC 3D Tiles into a **CesiumJS** web viewer. Everything is open
source and needs no Cesium ion or Google account.

## Run it locally

```bash
pip install -r pipeline/requirements.txt
bash pipeline/download.sh                      # ~2 GB into data/raw/
python pipeline/build_tiles.py --out build/tiles
python pipeline/build_surface.py --out build/surface
python pipeline/build_buildings.py --out build/buildings
ln -s ../build/tiles viewer/tiles
ln -s ../build/surface viewer/surface
ln -s ../build/buildings viewer/buildings
python -m http.server 8000 --directory viewer  # open http://localhost:8000
```

Two views: **Surface** (default) shows 8,000 3D buildings with straight walls
and LiDAR-shaped roofs textured with the IGN photo, on a ground model draped
with the 20 cm photo, lit by a late-morning sun with shadows. **Points** shows the LiDAR point cloud. Add `?mode=points`
to open in Points.

Add `?place=monte-carlo` (or `le rocher`, `port hercule`, `fontvieille`,
`larvotto`, `monaco`, `riviera`) to the URL to start at a given place.

**Tours** in the panel start guided flights. The first is the **Grand Prix
lap**: one lap of the Circuit de Monaco along the real route, with corner names,
play/pause and a progress bar you can drag. Add `?tour=lap` to start it straight
away. Tours register themselves with `MC3D.tours.register` (see `viewer/tours.js`).

**Legendary laps** rides along behind a small generic single-seater on the
three fastest Monaco laps of each decade since the 1950s (official pole laps
and race fastest laps). Each replay takes exactly the real lap time; the speed
in each corner is estimated from the shape of the road. A camera button
switches between the chase view and an onboard view. Add `?tour=legends` to
open the picker, or `?tour=legends&legend=1985-senna` to start one lap. During the laps the photoreal road is
replaced by a clean modelled track (asphalt, kerbs, barriers) and a modelled
tunnel, so the scan's parked cars and roadworks disappear (`viewer/clean-track.js`;
add `?clean=0` to see the raw scan).

## Publish it

`.github/workflows/pages.yml` rebuilds the tiles from IGN and publishes the viewer
on GitHub Pages. Enable Pages with "GitHub Actions" as the source first (Pages on
a private repository needs a paid GitHub plan).

## Layout

| Path | What it holds |
|---|---|
| `pipeline/tiles.txt` | the 14 IGN 1 km tiles covering Monaco |
| `pipeline/download.sh` | downloads LiDAR, orthophotos and the RAF20 geoid grid |
| `pipeline/build_tiles.py` | thins, colours, reprojects and tiles the point cloud |
| `pipeline/build_surface.py` | ground height tiles for the Surface view (surface model with buildings taken out) |
| `pipeline/build_buildings.py` | 3D buildings from IGN outlines + LiDAR roofs and ground |
| `pipeline/build_lap.py` | bakes the circuit route and its heights into `viewer/lap-route.js` (already committed) |
| `viewer/` | the CesiumJS viewer |
| `data/sources.md` | every data source with licence and date |
| `docs/crs.md` | coordinate and height choices |

## Point budget

The full scan of Monaco is about 430 million points. The default build keeps
4 million points per km² (about 50 million in total, 500 MB) so the result is
easy to host. Raise `--points-per-km2` for a denser city if the hosting allows.
