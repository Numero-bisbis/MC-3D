# Data sources

| Layer | Source | Licence | Date | Used for |
|---|---|---|---|---|
| Classified point cloud | IGN LiDAR HD, 14 COPC tiles (see `pipeline/tiles.txt`) | Etalab Open Licence 2.0 | flown May–June 2021, edition 2025-06-20 | the 3D city |
| Surface model (MNS) | IGN LiDAR HD MNS 50 cm via Géoplateforme WMS (`IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93`) | Etalab Open Licence 2.0 | derived from the 2021 flights | Surface view |
| Terrain model (MNT) | IGN LiDAR HD MNT 50 cm via Géoplateforme WMS | Etalab Open Licence 2.0 | 2021 flights | ground under buildings, wall bottoms |
| Building outlines | IGN BD TOPO `BDTOPO_V3:batiment` via WFS (covers Monaco) | Etalab Open Licence 2.0 | current edition | 3D buildings |
| Orthophoto | IGN BD ORTHO via Géoplateforme WMS/WMTS (`ORTHOIMAGERY.ORTHOPHOTOS`) | Etalab Open Licence 2.0 | latest IGN edition | point colours; ground imagery near Monaco |
| Geoid grid | IGN RAF20 (`fr_ign_RAF20.tif` from cdn.proj.org) | Etalab Open Licence 2.0 | 2020 | NGF-IGN69 heights to WGS84 ellipsoid |
| Circuit route | OpenStreetMap relation 148194 "Circuit de Monaco" via the OSM API | ODbL 1.0 (credit "© OpenStreetMap contributors") | fetched 2026-11-16 | Grand Prix lap tour |
| Film set (Monte-Carlo) | IGN LiDAR HD MNT/MNS 50 cm, BD TOPO outlines, BD ORTHO 50 cm (tells tiled, copper and flat roofs, trees and yachts apart) and 20 cm (the roofs' own photo), OpenStreetMap roads, gardens, pools, piers, trees, building floors, names and years; Wikidata years built, styles and floors (CC0) | Etalab 2.0, ODbL 1.0 and CC0 | fetched 2026-11-18 | `viewer/filmset/monte-carlo.glb` |
| Film set (Le Rocher) | Same open data, area extended south to Monaco-Ville; the Prince's Palace, the Oceanographic Museum, Saint-Nicolas Cathedral and Fort Antoine built piece by piece from BD TOPO plans, LiDAR roof levels, the 20 cm orthophoto and Mapillary street photos (2015, 2019) as a visual reference for their façades (IGN blurs its orthophoto over the Palace); old town façade colours sampled from Mapillary photos (`pipeline/facade_colours.py --rocher`), lanes, ramparts, cannons and garden paths from OpenStreetMap | Etalab 2.0, ODbL 1.0, CC-BY-SA 4.0 | fetched 2026-11-24 | `viewer/filmset/monte-carlo.glb` |
| Film set (Mareterra) | OpenStreetMap outline of L'Anse du Portier, buildings with floors and heights, park, pond, pools, paths (mapped after the December 2024 opening); BD TOPO 2024 heights where OSM has none; Mapillary 2025 street photos as a visual reference for the white slab-balcony look and paving | ODbL 1.0, Etalab 2.0, CC-BY-SA 4.0 | fetched 2026-11-20 | Mareterra in `viewer/filmset/monte-carlo.glb` (the 2021 LiDAR and 2022 photo show a building site) |
| Wide imagery | EOX Sentinel-2 cloudless 2020 | CC BY-NC-SA 4.0 (non-commercial) | 2020 | Riviera view |
| Wide terrain | AWS Terrain Tiles (Mapzen Terrarium) | mixed open sources, see tilezen/joerd attribution | — | mountains behind Monaco |

Notes
- The LiDAR HD coverage includes all of Monaco even though it is outside France.
  Tile list from WFS `IGNF_LIDAR-HD_METADONNEE:metadata`, field `coordonnees_nw`.
- The IGN download service is limited to about 1 request per second.
- EOX 2020 imagery is non-commercial; swap it for another source before any
  commercial use.
- Not open and not used: Monaco government digital twin (Siradel, 2024 rescan),
  Google Photorealistic 3D Tiles.
