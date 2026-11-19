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

**Legendary laps** rides along behind a single-seater shaped for its era (1950s cigar to today's halo cars) in the colours its team ran that year (colour schemes only, no sponsor marks) on the
three fastest Monaco laps of each decade since the 1950s (official pole laps
and race fastest laps). Each replay takes exactly the real lap time; the speed
in each corner is estimated from the shape of the road. A camera button
switches between the chase view and an onboard view. The car drives a real racing line rather than the middle of the road: a minimum-curvature line across the modelled track, outside on the way in, kerb at the apex, outside on the way out (add `?line=middle` to compare). A moon button switches the laps between day and night; it is the same switch as the film set's "☾ Night" (add `?night=1` to start at night). Add `?tour=legends` to
open the picker, or `?tour=legends&legend=1985-senna` to start one lap. During the laps the photoreal road is
replaced by a clean modelled track (asphalt, kerbs, barriers) and a modelled
tunnel, so the scan's parked cars and roadworks disappear (`viewer/clean-track.js`;
add `?clean=0` to see the raw scan). In the Surface view the ground model is
pressed down under the whole circuit, so distant, coarse terrain tiles cannot
fold the hillside over the road. The Nouvelle Chicane has no barrier on
the right as the cars arrive from the tunnel, through the whole chicane
until the road runs straight again; the left side keeps its rail.

**Street View stops**: in the lap tours, the pegman button pauses the lap and
opens Google's own 360° Street View panorama of the corner you are at; ‹ and ›
walk through all the corners. The Le Rocher tour has a Street View button at
each landmark. The panorama is Google's official viewer (Maps Embed API, free
with no usage limit), shown in an overlay; it is never copied onto the 3D
scene (`viewer/streetview.js`). The Google key needs **Maps Embed API**
enabled in its project and ticked in its API restrictions.

**Drive** (`?tour=drive`, or "Drive" in Tours) puts you in a car on the film
set, on the Grand Prix circuit: arrow keys or WASD, Space for the handbrake,
C for the camera, Esc to leave; on a phone, steering bottom left and pedals
bottom right. The car stays between the circuit's barriers, rides the modelled
track through the tunnel, and skips ahead past Port Hercule, which lies outside
the film set. A turning minimap with
the circuit, a speedometer and the names of the places you pass make up the
HUD (`viewer/drive.js`).

**Night** (the "☾ Night" button under View while the film set is on, the ☾
button while driving, or `?night=1`): the sun goes down and a cool moon
lights the set, about half the windows and every shop light up, 600 street
lamps traced along the OpenStreetMap streets glow over pools of light on
the pavement (a light map from the build, `monte-carlo-night.jpg`), the sky
fills with stars and the car gets headlights. Day or night, the film set
uses a game-style grade: richer colour, an S-curve, golden highlights over
teal shadows, haze that thickens with distance, and bloom (at night only on
phones).

**Film set** (`?mode=film`, or the "Film set" button in View) is our own
stylised model of Monte-Carlo, like a set for an animated film: Casino
square, the Boulingrins gardens, Port Hercule, La Condamine and the slopes
up to Beausoleil: the whole Grand Prix circuit, 1.5 × 1.5 km. Nothing in it comes from Google: shapes come
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
roofs top the palaces, tiled roofs are hipped, and most roofs (about 840)
wear the real 20 cm IGN aerial photo of themselves: tiles, skylights, rooftop
pools and terraces. The photo is not a true orthophoto, so taller roofs show
up shifted east by about 6 cm per metre of height; the build measures that
lean from the photo's edges and shifts each roof's patch back onto its
outline. Bridges are built from OpenStreetMap as decks at their LiDAR surface height,
with pavements, parapets and piers. Along the harbour the quays are kept flat
(the 2021 scan caught the Grand Prix grandstands as humps of ground). Roof
gardens are planted only where the photo shows green and the LiDAR sees
something standing on the roof. Nothing of the set stands on the Grand Prix track: trees, lamps and
building edges stop at its barriers, and the ground stays under the road and
below the barrier tops, so no slope spills over its edges, even where two stretches of the lap
run side by side at different heights (below the hairpin, Ostende above the
harbour). The build sweeps the whole lap and prints anything left standing on
the road (`track audit`). The tunnel is open
on its sea side where it runs along the shore: a parapet, pillars every 7.5 m
and the sea between them, with the hotel above on stilts. Ground floors along the streets are rows of shops (display windows lit
from inside, doors, painted sign bands with gold lettering, striped awnings),
and close up the ground map gains fine grain and paving joints. The Casino gets its twin corner towers with copper caps and gilded finials
and a raised centre with its clock on the square. Along the shore the sea
turns lighter, with a line of foam and small waves lapping at the quays.
Mapped street trees are sized from the LiDAR canopy (the plane trees of the
boulevards), and white shapes on piers and quays are no longer taken for
boats. Boats are lofted in 3D (hulls flaring from the waterline to the deck, the sheer rising to the bow, a boot stripe and bulwark, tapered and raked superstructure tiers with a band of dark glass, roof overhangs and masts) and moored the way Port Hercule moors them: stern-to, bow out,
square to the quay or pontoon and side by side (neighbours that merge into
one white shape in the photo are split into a row). Trees need a green crown
in the photo and at least 6 m² of it, and none grow on pools, pool decks,
sports grounds or piers. The Stade Nautique Rainier III gets its racing pool
(coping, lane lines, lane ropes) and its diving tower, with boards from 1 to
10 m. During the Legendary laps, about 5,500 spectators line the barriers
from the swimming pool to La Rascasse, on the start straight and at Sainte
Dévote, the Casino and Tabac, hopping out of step. Each is a smooth
low-poly person (shoes, trousers or shorts, a shirt, arms down or raised,
hair or a cap). They are hidden in the
plain film set view. The water is the dark green-grey teal of the port in
the IGN photo, with soft reflections. Walls weather subtly (patchy plaster, floor-to-floor tint, rain
streaks under the sills). Trees and moored
yachts are rebuilt from the LiDAR, flat roofs get parapets and the
machinery rooms the LiDAR sees and plants where the IGN photo shows roof
gardens, Belle Époque buildings get stone cornices, the streets carry the
sun shadows of buildings and trees (computed from the LiDAR for the scene's
sun, so they show on phones too), about 500 yachts traced from the IGN
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
