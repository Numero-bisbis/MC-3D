# Data sources

| Layer | Source | Licence | Date | Used for |
|---|---|---|---|---|
| Classified point cloud | IGN LiDAR HD, 14 COPC tiles (see `pipeline/tiles.txt`) | Etalab Open Licence 2.0 | flown May–June 2021, edition 2025-06-20 | the 3D city |
| Surface model (MNS) | IGN LiDAR HD MNS 50 cm via Géoplateforme WMS (`IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93`) | Etalab Open Licence 2.0 | derived from the 2021 flights | Surface view |
| Orthophoto | IGN BD ORTHO via Géoplateforme WMS/WMTS (`ORTHOIMAGERY.ORTHOPHOTOS`) | Etalab Open Licence 2.0 | latest IGN edition | point colours; ground imagery near Monaco |
| Geoid grid | IGN RAF20 (`fr_ign_RAF20.tif` from cdn.proj.org) | Etalab Open Licence 2.0 | 2020 | NGF-IGN69 heights to WGS84 ellipsoid |
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
