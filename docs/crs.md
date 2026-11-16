# Coordinates and heights

| Stage | System | Why |
|---|---|---|
| Source | Lambert-93 (EPSG:2154) + NGF-IGN69 heights (EPSG:5720) | native IGN system, metric, exact near France |
| Conversion | RAF20 geoid grid, via PROJ | IGN69 altitudes are about **48.9 m** lower than WGS84 ellipsoid heights at Monaco; skipping this makes the city float or sink |
| Storage | local east-north-up frame centred on 7.4246° E, 43.7384° N | keeps 16-bit quantized positions precise (about 1.5 cm in a 1 km tile) |
| Delivery | 3D Tiles root `transform` to Earth-centred (EPSG:4978) | lets Cesium zoom from the whole planet to a street without seams |

Tiling: root (sample of everything) → 1 km IGN tiles → 500 m → 250 m, additive
refinement, geometric errors 20 / 6 / 2 / 0 m.

The viewer terrain (Terrarium, heights above mean sea level) is shifted up by the
same 48.9 m so the ground, the sea and the LiDAR line up. This constant is exact
only around Monaco; the geoid changes by a few metres across the wider Riviera.
