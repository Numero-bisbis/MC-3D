#!/usr/bin/env bash
# Download the raw inputs: IGN LiDAR HD classified point clouds (COPC),
# IGN orthophotos for colour, and the RAF20 geoid grid.
# Everything lands in data/raw/ (git-ignored, about 2 GB).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RAW="$ROOT/data/raw"
mkdir -p "$RAW/lidar" "$RAW/ortho" "$RAW/grids" "$RAW/mns"

LIDAR_BASE="https://data.geopf.fr/telechargement/download/LiDARHD-NUALID/NUALHD_1-0__LAZ_LAMB93_SP_2025-06-20"
WMS="https://data.geopf.fr/wms-r"
ORTHO_PX="${ORTHO_PX:-4000}"   # 4000 px per km = 25 cm per pixel

# IGN services allow about 1 request per second and sometimes answer 400 when
# pushed; retry a few times with a pause.
fetch() {
  local out="$1" url="$2" i
  for i in 1 2 3 4 5; do
    if curl -fsS -o "$out.part" "$url"; then mv "$out.part" "$out"; return 0; fi
    sleep $((i * 3))
  done
  echo "failed: $url" >&2; return 1
}

# The IGN download service allows about 1 request per second.
for t in $(grep -v '^#' "$ROOT/pipeline/tiles.txt"); do
  x="${t%_*}"; y="${t#*_}"
  f="LHD_FXX_${t}_PTS_LAMB93_IGN69.copc.laz"
  if [ ! -s "$RAW/lidar/$f" ]; then
    echo "lidar $t"
    fetch "$RAW/lidar/$f" "$LIDAR_BASE/$f"
    sleep 1.2
  fi
  o="$RAW/ortho/ortho_${t}.jpg"
  if [ ! -s "$o" ]; then
    echo "ortho $t"
    xmin=$((x * 1000)); xmax=$((xmin + 1000)); ymax=$((y * 1000)); ymin=$((ymax - 1000))
    fetch "$o" \
      "$WMS?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=ORTHOIMAGERY.ORTHOPHOTOS&STYLES=&FORMAT=image/jpeg&CRS=EPSG:2154&BBOX=$xmin,$ymin,$xmax,$ymax&WIDTH=$ORTHO_PX&HEIGHT=$ORTHO_PX"
    sleep 1.2
  fi
  m="$RAW/mns/mns_${t}.tif"
  if [ ! -s "$m" ]; then
    # IGN LiDAR HD surface model (MNS), 50 cm, heights in NGF-IGN69.
    # Same box as IGN's own tile links: pixel centres on whole and half metres.
    echo "mns $t"
    fetch "$m" \
      "$WMS?SERVICE=WMS&VERSION=1.3.0&EXCEPTIONS=text/xml&REQUEST=GetMap&LAYERS=IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93&FORMAT=image/geotiff&STYLES=&CRS=EPSG:2154&BBOX=$((x * 1000 - 1)).75,$((y * 1000 - 1000)).25,$((x * 1000 + 999)).75,$((y * 1000)).25&WIDTH=2000&HEIGHT=2000"
    sleep 1.2
  fi
done

[ -s "$RAW/grids/fr_ign_RAF20.tif" ] || curl -fsS -o "$RAW/grids/fr_ign_RAF20.tif" https://cdn.proj.org/fr_ign_RAF20.tif
echo "done"
