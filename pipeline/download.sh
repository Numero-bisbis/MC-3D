#!/usr/bin/env bash
# Download the raw inputs: IGN LiDAR HD classified point clouds (COPC),
# IGN orthophotos for colour, and the RAF20 geoid grid.
# Everything lands in data/raw/ (git-ignored, about 2 GB).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RAW="$ROOT/data/raw"
mkdir -p "$RAW/lidar" "$RAW/ortho" "$RAW/grids"

LIDAR_BASE="https://data.geopf.fr/telechargement/download/LiDARHD-NUALID/NUALHD_1-0__LAZ_LAMB93_SP_2025-06-20"
WMS="https://data.geopf.fr/wms-r"
ORTHO_PX="${ORTHO_PX:-4000}"   # 4000 px per km = 25 cm per pixel

# The IGN download service allows about 1 request per second.
for t in $(grep -v '^#' "$ROOT/pipeline/tiles.txt"); do
  x="${t%_*}"; y="${t#*_}"
  f="LHD_FXX_${t}_PTS_LAMB93_IGN69.copc.laz"
  if [ ! -s "$RAW/lidar/$f" ]; then
    echo "lidar $t"
    curl -fsS --retry 4 -o "$RAW/lidar/$f" "$LIDAR_BASE/$f"
    sleep 1.2
  fi
  o="$RAW/ortho/ortho_${t}.jpg"
  if [ ! -s "$o" ]; then
    echo "ortho $t"
    xmin=$((x * 1000)); xmax=$((xmin + 1000)); ymax=$((y * 1000)); ymin=$((ymax - 1000))
    curl -fsS --retry 4 -o "$o" \
      "$WMS?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ORTHOIMAGERY.ORTHOPHOTOS&STYLES=&FORMAT=image/jpeg&CRS=EPSG:2154&BBOX=$xmin,$ymin,$xmax,$ymax&WIDTH=$ORTHO_PX&HEIGHT=$ORTHO_PX"
    sleep 1.2
  fi
done

[ -s "$RAW/grids/fr_ign_RAF20.tif" ] || curl -fsS -o "$RAW/grids/fr_ign_RAF20.tif" https://cdn.proj.org/fr_ign_RAF20.tif
echo "done"
