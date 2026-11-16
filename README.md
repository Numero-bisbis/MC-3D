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
ln -s ../build/tiles viewer/tiles
ln -s ../build/surface viewer/surface
python -m http.server 8000 --directory viewer  # open http://localhost:8000
```

Two views: **Surface** (default) drapes the 20 cm IGN photo over the 50 cm
LiDAR surface model, so roofs, streets and hills are solid; walls look
stretched up close. **Points** shows the LiDAR point cloud. Add `?mode=points`
to open in Points.

Add `?place=monte-carlo` (or `le rocher`, `port hercule`, `fontvieille`,
`larvotto`, `monaco`, `riviera`) to the URL to start at a given place.

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
| `pipeline/build_surface.py` | turns the 50 cm surface model into height tiles for the Surface view |
| `viewer/` | the CesiumJS viewer |
| `data/sources.md` | every data source with licence and date |
| `docs/crs.md` | coordinate and height choices |

## Point budget

The full scan of Monaco is about 430 million points. The default build keeps
5 million points per km² (about 60 million in total, 610 MB) so the result is
easy to host. Raise `--points-per-km2` for a denser city if the hosting allows.
