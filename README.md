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
switches between the chase view and an onboard view. The car drives a real racing line rather than the middle of the road: a minimum-curvature line across the modelled track, outside on the way in, kerb at the apex, outside on the way out (add `?line=middle` to compare). Add `?tour=legends` to
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

**Drive** (`?tour=drive`, or "Drive" in Tours) puts you in a car on the film
set, open-world style: arrow keys or WASD, Space for the handbrake, C for the
camera, Esc to leave; on a phone, steering bottom left and pedals bottom
right. The car follows the set's ground, goes through the Grand Prix tunnel
and stops against buildings and the sea, from a 1 m drive map the film set
build writes (`viewer/filmset/monte-carlo-drive.png`). A turning minimap with
the circuit, a speedometer and the names of the places you pass make up the
HUD (`viewer/drive.js`).

**Film set** (`?mode=film`, or the "Film set" button in View) is our own
stylised model of Monte-Carlo, like a set for an animated film: Casino
square, the Boulingrins gardens, the north side of Port Hercule and the slopes
up to Beausoleil, 1.2 × 1.2 km. Nothing in it comes from Google: shapes come
from IGN LiDAR HD and BD TOPO, streets and gardens from OpenStreetMap, and
every colour and texture is authored by us. It is one 7 MB file
(`viewer/filmset/monte-carlo.glb`) that loads once and then runs from memory,
so there is no tile streaming at all. Each building gets one of nine real
Monaco façade types (Belle Époque palace, arched Casino front, Riviera house
with shutters, painted villa, 1930s, 1960s-80s loggias, 1970s tower with
balcony bands, contemporary glass and fins, glass tower), chosen from its
year built (OpenStreetMap, Wikidata), its name and a short list of landmarks
around the square, and as many rows of windows as its real number of floors
(OpenStreetMap, Wikidata, BD TOPO) where open data has it. Mansard
roofs top the palaces, tiled roofs are hipped, trees and moored
yachts are rebuilt from the LiDAR, flat roofs get parapets and the
machinery rooms the LiDAR sees and plants where the IGN photo shows roof
gardens, Belle Époque buildings get stone cornices, the streets carry the
sun shadows of buildings and trees (computed from the LiDAR for the scene's
sun, so they show on phones too), about 200 yachts traced from the IGN
photo are rebuilt with hulls and stepped decks, and the sea moves out to the
horizon under a light film colour grade. Buildings
the 2021 scan does not see are dropped, except those IGN dates from 2022 on
(Mareterra and its neighbours), which keep their BD TOPO height. The
Grand Prix tunnel under the Fairmont is carved out (the LiDAR only sees its
roof): a deck over it, an open tube with road and walls inside, and the lap
tours' lit vault fits inside it. Rebuild it with
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
