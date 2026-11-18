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

**Street View stops**: in the lap tours, the pegman button pauses the lap and
opens Google's own 360° Street View panorama of the corner you are at; ‹ and ›
walk through all the corners. The Le Rocher tour has a Street View button at
each landmark. The panorama is Google's official viewer (Maps Embed API, free
with no usage limit), shown in an overlay; it is never copied onto the 3D
scene (`viewer/streetview.js`). The Google key needs **Maps Embed API**
enabled in its project and ticked in its API restrictions.

**Film set** (`?mode=film`, or the "Film set" button in View) is our own
stylised model of Monte-Carlo, like a set for an animated film: Casino
square, the Boulingrins gardens, the north side of Port Hercule and the slopes
up to Beausoleil, 1.2 × 1.2 km. Nothing in it comes from Google: shapes come
from IGN LiDAR HD and BD TOPO, streets and gardens from OpenStreetMap, and
every colour and texture is authored by us. It is one 7 MB file
(`viewer/filmset/monte-carlo.glb`) that loads once and then runs from memory,
so there is no tile streaming at all. Belle Époque buildings get shutters and
mansard roofs, tiled roofs are hipped, towers get glass, trees and moored
yachts are rebuilt from the LiDAR, flat roofs get parapets and the
machinery rooms the LiDAR sees and plants where the IGN photo shows roof
gardens, Belle Époque buildings get stone cornices, the streets carry the
sun shadows of buildings and trees (computed from the LiDAR for the scene's
sun, so they show on phones too), about 200 yachts traced from the IGN
photo are rebuilt with hulls and stepped decks, and the sea moves out to the
horizon under a light film colour grade. Buildings
the 2021 scan does not see are dropped, except those IGN dates from 2022 on
(Mareterra and its neighbours), which keep their BD TOPO height. Rebuild it with
`python pipeline/build_filmset.py --fetch` (about 15 seconds after the 50 MB
download; needs `npx` for the meshopt compression step).

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
| `pipeline/build_filmset.py` | builds the Monte-Carlo film set into `viewer/filmset/` (already committed) |
| `pipeline/build_lap.py` | bakes the circuit route and its heights into `viewer/lap-route.js` (already committed) |
| `viewer/` | the CesiumJS viewer |
| `data/sources.md` | every data source with licence and date |
| `docs/crs.md` | coordinate and height choices |

## Point budget

The full scan of Monaco is about 430 million points. The default build keeps
4 million points per km² (about 50 million in total, 500 MB) so the result is
easy to host. Raise `--points-per-km2` for a denser city if the hosting allows.
