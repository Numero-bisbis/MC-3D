// Monaco 3D viewer: CesiumJS, open data only, no Cesium ion account needed.
/* global Cesium */

// Geoid height (WGS84 ellipsoid minus mean sea level) at Monaco, from the
// IGN RAF20 grid: 48.9 m. Terrain and sea level below are shifted by it so
// they line up with the LiDAR, which is converted with the same grid.
const GEOID_N = 48.9;

const PLACES = [
  { name: "Riviera", lon: 7.4246, lat: 43.7384, h: 0, range: 90000, pitch: -28, heading: 340 },
  { name: "Monaco", lon: 7.4230, lat: 43.7375, h: 60, range: 3200, pitch: -32, heading: 330 },
  { name: "Monte-Carlo", lon: 7.4276, lat: 43.7393, h: 80, range: 650, pitch: -30, heading: 300 },
  { name: "Le Rocher", lon: 7.4205, lat: 43.7312, h: 100, range: 700, pitch: -28, heading: 20 },
  { name: "Port Hercule", lon: 7.4255, lat: 43.7348, h: 50, range: 900, pitch: -35, heading: 320 },
  { name: "Fontvieille", lon: 7.4165, lat: 43.7288, h: 50, range: 800, pitch: -32, heading: 30 },
  { name: "Larvotto", lon: 7.4365, lat: 43.7460, h: 50, range: 800, pitch: -30, heading: 280 },
];

const viewer = new Cesium.Viewer("globe", {
  baseLayer: false,
  baseLayerPicker: false,
  geocoder: false,
  homeButton: false,
  sceneModePicker: false,
  navigationHelpButton: false,
  animation: false,
  timeline: false,
  fullscreenButton: false,
  infoBox: false,
  selectionIndicator: false,
  terrainProvider: makeTerrain(),
});
const scene = viewer.scene;
scene.globe.baseColor = Cesium.Color.fromCssColorString("#0d2236");
scene.globe.enableLighting = false;
scene.skyAtmosphere.show = true;
scene.fog.enabled = true;
scene.postProcessStages.fxaa.enabled = true;
if (Cesium.FeatureDetection.supportsImageRenderingPixelated()) {
  viewer.resolutionScale = Math.min(window.devicePixelRatio, 2);
}

// Imagery: Sentinel-2 cloudless for the wide view, IGN 20 cm orthophoto near Monaco.
viewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
  url: "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg",
  maximumLevel: 14,
  credit: "Sentinel-2 cloudless 2020 by EOX",
}));
viewer.imageryLayers.add(new Cesium.ImageryLayer(new Cesium.WebMapTileServiceImageryProvider({
  url: "https://data.geopf.fr/wmts",
  layer: "ORTHOIMAGERY.ORTHOPHOTOS",
  style: "normal",
  format: "image/jpeg",
  tileMatrixSetID: "PM",
  maximumLevel: 19,
  rectangle: Cesium.Rectangle.fromDegrees(7.2, 43.62, 7.6, 43.85),
  credit: "IGN orthophoto",
}), { minimumTerrainLevel: 11 }));

// Terrain: AWS/Mapzen Terrarium tiles (open), decoded into a heightmap.
function makeTerrain() {
  const scheme = new Cesium.GeographicTilingScheme();
  const cache = new Map();
  const SIZE = 33;

  function loadTile(z, x, y) {
    const key = `${z}/${x}/${y}`;
    if (!cache.has(key)) {
      const url = `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/${key}.png`;
      cache.set(key, Cesium.Resource.fetchImage({ url, preferImageBitmap: true }).then((img) => {
        const c = document.createElement("canvas");
        c.width = img.width; c.height = img.height;
        const ctx = c.getContext("2d", { willReadFrequently: true });
        ctx.drawImage(img, 0, 0);
        return ctx.getImageData(0, 0, img.width, img.height).data;
      }).catch(() => null));
    }
    return cache.get(key);
  }

  return new Cesium.CustomHeightmapTerrainProvider({
    width: SIZE,
    height: SIZE,
    tilingScheme: scheme,
    callback: async (x, y, level) => {
      const rect = scheme.tileXYToRectangle(x, y, level);
      const z = Math.min(level + 1, 13);
      const n = 2 ** z;
      const samples = [];
      const need = new Map();
      for (let j = 0; j < SIZE; j++) {
        const lat = rect.north - (rect.north - rect.south) * (j / (SIZE - 1));
        for (let i = 0; i < SIZE; i++) {
          const lon = rect.west + (rect.east - rect.west) * (i / (SIZE - 1));
          const fx = ((Cesium.Math.toDegrees(lon) + 180) / 360) * n;
          const s = Math.sin(Math.max(Math.min(lat, 1.484), -1.484));
          const fy = (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n;
          const tx = Math.min(Math.floor(fx), n - 1), ty = Math.min(Math.floor(fy), n - 1);
          const key = `${tx}/${ty}`;
          if (!need.has(key)) need.set(key, loadTile(z, tx, ty));
          samples.push([key, Math.min(255, Math.floor((fx - tx) * 256)), Math.min(255, Math.floor((fy - ty) * 256))]);
        }
      }
      const tiles = new Map();
      await Promise.all([...need].map(async ([k, p]) => tiles.set(k, await p)));
      const out = new Float32Array(SIZE * SIZE);
      samples.forEach(([k, px, py], idx) => {
        const d = tiles.get(k);
        let h = 0;
        if (d) {
          const o = (py * 256 + px) * 4;
          h = d[o] * 256 + d[o + 1] + d[o + 2] / 256 - 32768;
        }
        out[idx] = Math.max(h, 0) + GEOID_N; // sea floor is flattened to the sea surface
      });
      return out;
    },
  });
}

// The LiDAR point cloud.
let tileset;
const STYLES = {
  photo: undefined,
  class: new Cesium.Cesium3DTileStyle({
    color: {
      conditions: [
        ["${classification} === 6 || ${classification} === 67", "color('#e9e3d6')"],
        ["${classification} >= 3 && ${classification} <= 5", "color('#5f9c4f')"],
        ["${classification} === 9", "color('#3d7fb8')"],
        ["${classification} === 2", "color('#b59f7e')"],
        ["${classification} === 17", "color('#c9773a')"],
        ["true", "color('#8b8f96')"],
      ],
    },
  }),
  height: new Cesium.Cesium3DTileStyle({
    defines: { alt: `\${POSITION}.z - ${GEOID_N}` },
    color: {
      conditions: [
        ["${alt} < 2", "color('#1f4e79')"],
        ["${alt} < 30", "color('#2e8b8b')"],
        ["${alt} < 70", "color('#7bbf6a')"],
        ["${alt} < 120", "color('#e2d36b')"],
        ["${alt} < 200", "color('#e89a4c')"],
        ["true", "color('#d9534f')"],
      ],
    },
  }),
};

Cesium.Cesium3DTileset.fromUrl("tiles/tileset.json", {
  maximumScreenSpaceError: 2,
  pointCloudShading: {
    attenuation: true,
    maximumAttenuation: 4,
    baseResolution: 0.8,
    eyeDomeLighting: true,
    eyeDomeLightingStrength: 0.6,
    eyeDomeLightingRadius: 1.0,
  },
}).then((ts) => {
  tileset = ts;
  scene.primitives.add(ts);
}).catch((e) => console.error("tileset failed", e));

// Camera helpers.
function view(p, duration) {
  const target = Cesium.Cartesian3.fromDegrees(p.lon, p.lat, p.h + GEOID_N);
  const offset = new Cesium.HeadingPitchRange(
    Cesium.Math.toRadians(p.heading), Cesium.Math.toRadians(p.pitch), p.range);
  viewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(target, 1), { offset, duration });
}

const places = document.getElementById("places");
for (const p of PLACES) {
  const b = document.createElement("button");
  b.textContent = p.name;
  b.onclick = () => view(p, 3);
  places.appendChild(b);
}

document.querySelectorAll("[data-style]").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("[data-style]").forEach((o) => o.classList.toggle("on", o === b));
    if (tileset) tileset.style = STYLES[b.dataset.style];
    document.getElementById("legend").style.display = b.dataset.style === "class" ? "block" : "none";
  };
});

// Start outside, over the Riviera, then glide into Monaco.
const params = new URLSearchParams(location.search);
const start = PLACES.find((p) => p.name.toLowerCase() === (params.get("place") || "").toLowerCase());
if (start) {
  view(start, 0);
} else {
  view(PLACES[0], 0);
  setTimeout(() => view(PLACES[1], 6), 1500);
}
window.__viewer = viewer;
