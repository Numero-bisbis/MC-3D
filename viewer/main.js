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
  terrainProvider: makeTerrain(null),
});
const scene = viewer.scene;
scene.globe.baseColor = Cesium.Color.fromCssColorString("#0d2236");
// Light: a late-morning summer sun over Monaco (11:30 local time, from the
// south-east, the sea side), so the façades seen from the sea are lit.
const IS_SMALL = window.matchMedia("(pointer: coarse)").matches || window.innerWidth < 700;
viewer.clock.currentTime = Cesium.JulianDate.fromIso8601("2026-06-21T09:30:00Z");
viewer.clock.shouldAnimate = false;
scene.globe.enableLighting = true;
scene.globe.dynamicAtmosphereLighting = true;
scene.globe.showGroundAtmosphere = true;
scene.skyAtmosphere.show = true;
scene.fog.enabled = true;
scene.fog.density = 0.00012;
scene.highDynamicRange = !IS_SMALL;
if (!IS_SMALL) {
  // Shadows are costly on phones; on desktop the surface model casts them,
  // so buildings and the Rock throw real shadows.
  viewer.shadows = true;
  viewer.terrainShadows = Cesium.ShadowMode.ENABLED;
  viewer.shadowMap.softShadows = true;
  viewer.shadowMap.size = 4096;
  viewer.shadowMap.maximumDistance = 4000;
  viewer.shadowMap.darkness = 0.55;
}
scene.postProcessStages.fxaa.enabled = true;
// Resolution. Rendering at the full pixel density of a phone screen (3x on
// iPhones) costs up to 9 times the work of 1x and was the main cause of
// stutter. Draw at a lighter scale while the camera moves and sharpen once
// it stops for a moment.
const SHARP_SCALE = Cesium.FeatureDetection.supportsImageRenderingPixelated()
  ? Math.min(window.devicePixelRatio, IS_SMALL ? 1.5 : 2) : 1;
const MOVING_SCALE = IS_SMALL ? 1 : Math.min(SHARP_SCALE, 1.5);
viewer.resolutionScale = SHARP_SCALE;
// A tour that keeps the camera moving all the time (the lap tours) can hold
// one steady quality instead, see setTourQuality below.
let tourQ = null;
{
  let idle;
  viewer.camera.moveStart.addEventListener(() => {
    clearTimeout(idle);
    if (tourQ) return;
    if (viewer.resolutionScale !== MOVING_SCALE) viewer.resolutionScale = MOVING_SCALE;
    if (google) google.maximumScreenSpaceError = GOOGLE_SSE.moving;
  });
  viewer.camera.moveEnd.addEventListener(() => {
    clearTimeout(idle);
    if (tourQ) return;
    idle = setTimeout(() => {
      viewer.resolutionScale = SHARP_SCALE;
      if (google) google.maximumScreenSpaceError = GOOGLE_SSE.still;
      scene.requestRender();
    }, 400);
  });
}

// Draw only when something changes. Cesium redraws by itself when the
// camera moves, a flight or inertia runs, or tiles and models arrive; a
// still view then costs nothing, instead of redrawing the whole city 60
// times a second, which kept phones hot and throttled and made every
// gesture start late. Things that animate on their own ask for frames with
// MC3D.keepRendering(key, fps): fps 0 = every frame, false = stop. Tours
// (body.touring) always get every frame.
scene.requestRenderMode = true;
scene.maximumRenderTimeChange = Infinity;
const keepAlive = new Map();
function keepRendering(key, fps) {
  if (fps === false || fps === undefined || fps === null) keepAlive.delete(key);
  else keepAlive.set(key, fps);
  scene.requestRender();
}
{
  let lastReq = 0;
  const pump = (now) => {
    let interval = Infinity;
    if (document.body.classList.contains("touring")) interval = 0;
    for (const f of keepAlive.values()) interval = Math.min(interval, f > 0 ? 1000 / f : 0);
    if (now - lastReq >= interval - 4) { scene.requestRender(); lastReq = now; }
    requestAnimationFrame(pump);
  };
  requestAnimationFrame(pump);
  // A button or key can change the scene without moving the camera (night,
  // a view mode, a tour step): keep drawing for a moment after any input.
  let uiTimer;
  const poke = () => {
    keepRendering("ui", 0);
    clearTimeout(uiTimer);
    uiTimer = setTimeout(() => keepRendering("ui", false), 1500);
  };
  for (const ev of ["click", "keydown", "keyup", "pointerdown", "pointerup", "wheel", "touchstart", "touchend"]) {
    document.addEventListener(ev, poke, { capture: true, passive: true });
  }
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
}), {
  minimumTerrainLevel: 11,
  // Out at sea the IGN photo stops in blank white squares; make those
  // see-through so the satellite image of the sea shows instead.
  colorToAlpha: Cesium.Color.WHITE,
  colorToAlphaThreshold: 0.03,
}));

// Terrain. Everywhere: AWS/Mapzen Terrarium tiles (open), decoded into a
// heightmap. Over Monaco, in "Surface" mode: the IGN LiDAR HD surface model
// (50 cm, roofs and trees included) from surface/, built by
// pipeline/build_surface.py. The orthophoto drapes over it.
function makeTerrain(surface) {
  const scheme = new Cesium.GeographicTilingScheme();
  const cache = new Map();
  const SIZE = 65;

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

  async function terrarium(x, y, level) {
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
  }

  function surfaceUrl(x, y, level) {
    if (!surface || level < surface.minLevel) return null;
    const r = surface.levels[Math.min(level, surface.maxLevel)];
    if (level > surface.maxLevel) return null;
    if (x < r[0] || x > r[1] || y < r[2] || y > r[3]) return null;
    return `surface/${level}/${x}/${y}.bin`;
  }

  return new Cesium.CustomHeightmapTerrainProvider({
    width: SIZE,
    height: SIZE,
    tilingScheme: scheme,
    callback: async (x, y, level) => {
      const url = surfaceUrl(x, y, level);
      if (!url) return terrarium(x, y, level);
      let enc = null;
      try { enc = new Uint16Array(await Cesium.Resource.fetchArrayBuffer({ url })); } catch (e) { /* tile not built */ }
      if (!enc || enc.length !== SIZE * SIZE) return terrarium(x, y, level);
      const out = new Float32Array(SIZE * SIZE);
      let fallback = null;
      for (let i = 0; i < out.length; i++) {
        if (enc[i]) { out[i] = enc[i] / 50 - 100; continue; }
        fallback = fallback || await terrarium(x, y, level);
        out[i] = fallback[i];
      }
      // Keep the circuit's road clear of trees and awnings in the surface
      // model (clean-track.js sets the hook).
      if (window.MC3D && window.MC3D.roadClamp) window.MC3D.roadClamp(scheme.tileXYToRectangle(x, y, level), SIZE, out);
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

// Phones get a lighter load; desktops load the full density.
const MOBILE = window.matchMedia("(pointer: coarse)").matches || window.innerWidth < 700;

let pointsLoading = null;
function loadPoints() {
  if (pointsLoading) return pointsLoading;
  pointsLoading = Cesium.Cesium3DTileset.fromUrl("tiles/tileset.json", {
  maximumScreenSpaceError: MOBILE ? 3 : 1.5,
  cacheBytes: (MOBILE ? 384 : 1536) * 1024 * 1024,
  maximumCacheOverflowBytes: (MOBILE ? 128 : 512) * 1024 * 1024,
  pointCloudShading: {
    // Points grow with distance only up to 2.5 px, so close up they stay
    // small and sharp instead of turning into blocks.
    attenuation: true,
    maximumAttenuation: 2.5,
    baseResolution: 0.35,
    eyeDomeLighting: true,
    eyeDomeLightingStrength: 0.9,
    eyeDomeLightingRadius: 0.8,
  },
  }).then((ts) => {
    tileset = ts;
    watchLoading(ts);
    ts.show = mode === "points" && !document.body.classList.contains("photoreal") && !openHidden;
    scene.primitives.add(ts);
    const active = document.querySelector("[data-style].on");
    if (active) ts.style = STYLES[active.dataset.style];
  }).catch((e) => console.error("tileset failed", e));
  return pointsLoading;
}

// Optional photoreal layer: Google Photorealistic 3D Tiles. Needs a Google
// Maps Platform key with the Map Tiles API enabled, restricted to this site.
// Set it in config.js, or add ?gkey=YOUR_KEY to the URL to try one.
const GOOGLE_KEY = new URLSearchParams(location.search).get("gkey") ||
  (window.MC3D_CONFIG && window.MC3D_CONFIG.googleKey) || "";
// A small "Loading" pill while tiles stream in, shown only if loading lasts
// more than half a second, so short top-ups do not flicker.
const loadingEl = document.getElementById("loading");
const pending = new Map();
let loadingTimer;
function setPending(key, n) {
  pending.set(key, n);
  const busy = [...pending.values()].some((v) => v > 0);
  if (busy && !loadingTimer && loadingEl.hidden) {
    loadingTimer = setTimeout(() => { loadingEl.hidden = false; loadingTimer = null; }, 500);
  } else if (!busy) {
    clearTimeout(loadingTimer); loadingTimer = null;
    loadingEl.hidden = true;
  }
}
function watchLoading(ts) {
  ts.loadProgress.addEventListener((reqs, processing) => setPending(ts, reqs + processing));
}
scene.globe.tileLoadProgressEvent.addEventListener((n) => setPending("globe", scene.globe.show ? n : 0));

let google;
const GOOGLE_CACHE = { bytes: (MOBILE ? 256 : 1024) * 1024 * 1024, overflow: (MOBILE ? 64 : 512) * 1024 * 1024 };
// Google detail: coarser while the camera moves (fewer downloads, smoother
// motion), finer once it stops, so a still view is sharper than before.
const GOOGLE_SSE = MOBILE ? { moving: 24, still: 12 } : { moving: 12, still: 6 };
async function setPhotoreal(on) {
  if (on && !google) {
    google = await Cesium.createGooglePhotorealistic3DTileset(GOOGLE_KEY, {
      // A cache that stays clear of iOS Safari's memory limit on phones, and
      // less detail far from the screen centre and while moving.
      maximumScreenSpaceError: GOOGLE_SSE.still,
      cacheBytes: GOOGLE_CACHE.bytes,
      maximumCacheOverflowBytes: GOOGLE_CACHE.overflow,
      dynamicScreenSpaceError: true,
      foveatedScreenSpaceError: true,
      cullRequestsWhileMoving: true,
      preloadFlightDestinations: true,
    });
    scene.primitives.add(google);
    watchLoading(google);
    if (tourQ) setTourQuality(true);
  }
  if (google) google.show = on;
  if (tileset) tileset.show = !on && mode === "points";
  if (buildings) buildings.show = !on && mode === "surface";
  scene.globe.show = !on;   // Google's mesh already includes the terrain
  document.body.classList.toggle("photoreal", on);
}
// Street-level tours: one steady level of Google detail while the camera
// keeps moving (the usual "coarser while moving" would leave the whole lap
// blurry), a larger cache so the circuit is not thrown away and fetched
// again, and requests kept even at speed. null puts everything back.
// Down at street level most of what a wide view takes in is far away: a
// much stronger distance fall-off (dynamic screen-space error) keeps the
// near streets sharp while far blocks come in coarse, which cuts the number
// of tiles to fetch per second. More requests may run at once, too.
const DYN = { density: 2e-4, factor: 24 };
const REQ = { perServer: Cesium.RequestScheduler.maximumRequestsPerServer, total: Cesium.RequestScheduler.maximumRequests };
// Bandwidth varies a lot from one phone and network to the next, so the
// detail adapts while a lap plays: when downloads fall behind (many tiles
// requested or still being decoded) the buildings get coarser right away,
// and once the queue has been short for a while detail comes back step by
// step. The road and the car are modelled, so they stay sharp either way.
const TOUR_SSE = MOBILE ? { base: 24, max: 64 } : { base: 8, max: 32 };
const BACKLOG = { high: MOBILE ? 16 : 24, low: MOBILE ? 6 : 8 };
let adaptAt = 0, calmSince = 0;
function adaptTour() {
  if (!tourQ || !google || !google.show) return;
  const now = performance.now();
  if (now - adaptAt < 250) return;
  adaptAt = now;
  const st = google.statistics || google._statistics;
  if (!st) return;
  const backlog = st.numberOfPendingRequests + st.numberOfTilesProcessing;
  let sse = tourQ.sse;
  if (backlog > BACKLOG.high) {
    sse = Math.min(TOUR_SSE.max, sse * 1.3);
    calmSince = now;
  } else if (backlog > BACKLOG.low) {
    calmSince = now;
  } else if (now - calmSince > 1500) {
    sse = Math.max(TOUR_SSE.base, sse / 1.1);
    calmSince = now - 1000;   // next step back after another half second
  }
  tourQ.sse = sse;
  applyTourDetail();
}
// Speed matters too: at 80 m/s a lap runs through new streets almost three
// times faster than the Grand Prix lap tour's 30 m/s, so it needs about
// three times as many tiles a second. The legendary laps report their speed
// and the detail drops with it (back to normal at 30 m/s and below).
const SPEED_REF = 30;
function applyTourDetail() {
  if (!tourQ || !google) return;
  const mul = Math.min(2.2, Math.max(1, (tourQ.speed / SPEED_REF) ** 0.75));
  const eff = tourQ.sse * mul;
  if (Math.abs(eff - google.maximumScreenSpaceError) > 0.02 * eff) google.maximumScreenSpaceError = eff;
}
// The detail follows the fast parts of the lap, not every corner: it gets
// coarser at once when the car speeds up but only comes back over a few
// seconds after it slows. Following the speed exactly made the scenery
// switch between fine and coarse tiles at every braking point, and the
// fine tiles asked for in a slow corner arrived after the car had left it.
function setTourSpeed(v) {
  if (!tourQ) return;
  v = v || 0;
  const now = performance.now(), dt = Math.min(1, (now - (tourQ.at || now)) / 1000);
  tourQ.at = now;
  if (v === 0 || v >= tourQ.speed) tourQ.speed = v;
  else tourQ.speed += (v - tourQ.speed) * (1 - Math.exp(-dt / 4));
}
viewer.scene.postRender.addEventListener(adaptTour);
function setTourQuality(on) {
  tourQ = on ? { sse: TOUR_SSE.base, speed: 0, scale: MOBILE ? 1 : MOVING_SCALE } : null;
  calmSince = performance.now();
  viewer.resolutionScale = tourQ ? tourQ.scale : SHARP_SCALE;
  Cesium.RequestScheduler.maximumRequestsPerServer = tourQ ? 36 : REQ.perServer;
  Cesium.RequestScheduler.maximumRequests = tourQ ? 72 : REQ.total;
  if (!google) return;
  google.maximumScreenSpaceError = tourQ ? tourQ.sse : GOOGLE_SSE.still;
  google.cullRequestsWhileMoving = !tourQ;
  // "Foveated" loading puts off every tile away from the centre of the
  // screen until the camera has been still for a moment. In a lap the
  // camera is never still, so the buildings at the sides never sharpened:
  // the main cause of the endless loading at street level.
  google.foveatedScreenSpaceError = !tourQ;
  google.cacheBytes = tourQ ? GOOGLE_CACHE.bytes * 1.5 : GOOGLE_CACHE.bytes;
  google.maximumCacheOverflowBytes = tourQ ? GOOGLE_CACHE.overflow * 2 : GOOGLE_CACHE.overflow;
  google.dynamicScreenSpaceError = true;
  google.dynamicScreenSpaceErrorDensity = tourQ ? (MOBILE ? 3e-3 : 2.2e-3) : DYN.density;
  google.dynamicScreenSpaceErrorFactor = tourQ ? (MOBILE ? 64 : 48) : DYN.factor;
}
const realBtn = document.getElementById("photoreal");
// Photoreal is a third view next to Surface and Points: picking it lights its
// button and dims the others; picking Surface or Points turns it off.
async function choosePhotoreal(on) {
  realBtn.classList.toggle("on", on);
  if (on) document.querySelectorAll("[data-mode]").forEach((o) => o.classList.remove("on"));
  try {
    await setPhotoreal(on);
  } catch (e) {
    console.error("Google 3D Tiles failed", e);
    realBtn.classList.remove("on");
    realBtn.textContent = "Photoreal unavailable";
    realBtn.disabled = true;
    await setPhotoreal(false).catch(() => {});
    setMode(mode);
  }
}
if (GOOGLE_KEY) {
  realBtn.hidden = false;
  realBtn.onclick = () => choosePhotoreal(true);
}

// 3D buildings with straight walls (pipeline/build_buildings.py), shown in
// the Surface view on top of the ground model.
let buildings;
Cesium.Cesium3DTileset.fromUrl("buildings/tileset.json", {
  maximumScreenSpaceError: MOBILE ? 8 : 4,
  // Buildings cast shadows on the ground but do not receive them, which
  // avoids striped "shadow acne" on flat walls.
  shadows: Cesium.ShadowMode.CAST_ONLY,
}).then((ts) => {
  buildings = ts;
  watchLoading(ts);
  ts.show = mode === "surface" && !document.body.classList.contains("photoreal") && !openHidden;
  scene.primitives.add(ts);
}).catch((e) => console.error("buildings failed", e));

// View mode: "surface" = IGN surface model draped with the 20 cm photo,
// "points" = the LiDAR point cloud.
let mode = new URLSearchParams(location.search).get("mode") === "points" ? "points" : "surface";
const surfaceIndex = Cesium.Resource.fetchJson({ url: "surface/index.json" }).catch(() => null);
const pointsTerrain = viewer.terrainProvider;
let surfaceTerrain;
async function setMode(m) {
  mode = m;
  if (openHidden) return;
  document.querySelectorAll("[data-mode]").forEach((o) => o.classList.toggle("on", o.dataset.mode === m));
  document.getElementById("colour-group").hidden = m !== "points";
  if (m === "surface") {
    const idx = await surfaceIndex;
    if (idx) {
      surfaceTerrain = surfaceTerrain || makeTerrain(idx);
      scene.terrainProvider = surfaceTerrain;
    }
  } else {
    scene.terrainProvider = pointsTerrain;
    loadPoints();
  }
  const photoOn = document.body.classList.contains("photoreal");
  if (tileset) tileset.show = m === "points" && !photoOn;
  if (buildings) buildings.show = m === "surface" && !photoOn;
}
// The film set (filmset.js) hides the open-data layers and keeps only the
// smooth wide terrain and imagery around its own model.
let openHidden = false;
function hideOpenData(h) {
  openHidden = h;
  if (h) {
    scene.terrainProvider = pointsTerrain;
    if (tileset) tileset.show = false;
    if (buildings) buildings.show = false;
  } else {
    setMode(mode);
  }
}
document.querySelectorAll("[data-mode]").forEach((b) => {
  b.onclick = async () => {
    if (document.body.classList.contains("photoreal")) await choosePhotoreal(false);
    setMode(b.dataset.mode);
  };
});
setMode(mode);
// With a Google key, open in the photoreal view unless a view was asked for.
if (GOOGLE_KEY && !new URLSearchParams(location.search).get("mode")) {
  document.body.classList.add("photoreal");
  scene.globe.show = false;
  choosePhotoreal(true);
}

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

// Opening shot. In the photoreal view, start just outside Monaco and glide
// in: starting 90 km out over the Riviera made Google stream the whole coast
// first, which was most of the wait. The open-data views keep the Riviera
// opening.
const params = new URLSearchParams(location.search);
const start = PLACES.find((p) => p.name.toLowerCase() === (params.get("place") || "").toLowerCase());
if (start) {
  view(start, 0);
} else if (params.get("tour")) {
  view(PLACES[1], 0);
} else if (document.body.classList.contains("photoreal")) {
  view({ ...PLACES[1], range: 6500, pitch: -24 }, 0);
  setTimeout(() => view(PLACES[1], 4), 1200);
} else {
  view(PLACES[0], 0);
  setTimeout(() => view(PLACES[1], 6), 1500);
}
// ---- Navigation -------------------------------------------------------
// Mouse: drag = move, right-drag (or Ctrl/Shift + drag) = turn and tilt,
// wheel = zoom, double-click = fly to that spot.
// Touch: one finger = move, two fingers = pinch to zoom, twist to turn,
// slide up/down to tilt, double-tap = fly to that spot.
const ctrl = scene.screenSpaceCameraController;
const E = Cesium.CameraEventType, K = Cesium.KeyboardEventModifier;
ctrl.zoomEventTypes = [E.WHEEL, E.PINCH];
ctrl.tiltEventTypes = [E.RIGHT_DRAG, E.MIDDLE_DRAG, E.PINCH,
  { eventType: E.LEFT_DRAG, modifier: K.CTRL }, { eventType: E.LEFT_DRAG, modifier: K.SHIFT }];
// Never closer than 60 m above the city (closer, the open data falls apart
// and you end up inside buildings), never further than the whole of Europe.
ctrl.minimumZoomDistance = 60;
ctrl.maximumZoomDistance = 2500000;
ctrl.enableCollisionDetection = true;
ctrl.inertiaSpin = 0.85; ctrl.inertiaTranslate = 0.85; ctrl.inertiaZoom = 0.7;

const cam = viewer.camera;
const handler = viewer.cesiumWidget.screenSpaceEventHandler;
handler.removeInputAction(Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

function pickGround(pos) {
  const ray = cam.getPickRay(pos);
  return (scene.pickPositionSupported && scene.pickPosition(pos)) || scene.globe.pick(ray, scene);
}
function screenCenter() {
  const c = scene.canvas;
  return pickGround(new Cesium.Cartesian2(c.clientWidth / 2, c.clientHeight / 2));
}
// Turn, tilt or zoom around the point in the middle of the screen.
function orbit(dHeading, dPitch, zoomFactor) {
  const target = screenCenter();
  if (!target) {               // looking at the sky: just zoom along the view
    if (zoomFactor) cam.zoomIn((1 - zoomFactor) * cam.positionCartographic.height);
    return;
  }
  const range = Cesium.Cartesian3.distance(cam.position, target);
  const heading = cam.heading + Cesium.Math.toRadians(dHeading);
  const pitch = Cesium.Math.clamp(cam.pitch + Cesium.Math.toRadians(dPitch),
    Cesium.Math.toRadians(-89), Cesium.Math.toRadians(-8));
  const newRange = Cesium.Math.clamp(range * (zoomFactor || 1), ctrl.minimumZoomDistance + 20, ctrl.maximumZoomDistance);
  cam.flyToBoundingSphere(new Cesium.BoundingSphere(target, 1), {
    offset: new Cesium.HeadingPitchRange(heading, pitch, newRange), duration: 0.6,
  });
}
const on = (id, fn) => { document.getElementById(id).onclick = fn; };
on("nav-zoom-in", () => orbit(0, 0, 0.5));
on("nav-zoom-out", () => orbit(0, 0, 2));
on("nav-left", () => orbit(-30, 0));
on("nav-right", () => orbit(30, 0));
on("nav-tilt-up", () => orbit(0, -12));
on("nav-tilt-down", () => orbit(0, 12));
on("compass", () => orbit(-Cesium.Math.toDegrees(cam.heading), 0));
on("nav-home", () => view(PLACES[1], 2));

// Double-click or double-tap: glide to that spot, keeping the direction.
handler.setInputAction((e) => {
  const target = pickGround(e.position);
  if (!target) return;
  const range = Math.max(250, Math.min(Cesium.Cartesian3.distance(cam.position, target) * 0.5, 4000));
  cam.flyToBoundingSphere(new Cesium.BoundingSphere(target, 1), {
    offset: new Cesium.HeadingPitchRange(cam.heading, Math.min(cam.pitch, Cesium.Math.toRadians(-25)), range),
    duration: 1.2,
  });
}, Cesium.ScreenSpaceEventType.LEFT_DOUBLE_CLICK);

// Compass needle follows the view direction.
const needle = document.querySelector("#compass svg");
cam.percentageChanged = 0.01;
cam.changed.addEventListener(() => {
  needle.style.transform = `rotate(${-Cesium.Math.toDegrees(cam.heading)}deg)`;
});

// One-line help, gone after the first interaction or 12 seconds.
const hint = document.getElementById("hint");
hint.textContent = MOBILE
  ? "One finger to move · pinch to zoom · two fingers to turn and tilt · double-tap to fly there"
  : "Drag to move · right-drag to turn and tilt · scroll to zoom · double-click to fly there";
const hideHint = () => hint.classList.add("gone");
setTimeout(hideHint, 12000);
scene.canvas.addEventListener("pointerdown", () => setTimeout(hideHint, 4000), { once: true });

// The menu folds into a small "Menu" button. On phones it folds away by
// itself once you pick a place or a view, or start moving around, so the
// city gets the whole screen; one tap brings it back.
const panel = document.getElementById("panel");
const panelToggle = document.getElementById("panel-toggle");
function setPanel(open) {
  panel.classList.toggle("collapsed", !open);
  panelToggle.setAttribute("aria-expanded", String(open));
  panelToggle.firstChild.textContent = open ? "Hide" : "Menu";
  panelToggle.lastChild.textContent = open ? "▴" : "▾";
}
panelToggle.onclick = () => setPanel(panel.classList.contains("collapsed"));
if (MOBILE) {
  document.getElementById("panel-body").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    // Tours hide the menu anyway; the colour buttons are a quick switch.
    if (b && !b.dataset.style && !b.classList.contains("tour-btn")) setTimeout(() => setPanel(false), 250);
  });
  scene.canvas.addEventListener("pointerdown", () => setPanel(false));
}

window.__viewer = viewer;

// Shared handle for add-ons loaded after this file (tours.js, lap.js).
window.MC3D = {
  viewer, view, PLACES, GEOID_N, setMode, getMode: () => mode, setTourQuality, setTourSpeed,
  hideOpenData, keepRendering,
  setPhotorealOff: () => (document.body.classList.contains("photoreal") ? choosePhotoreal(false) : null),
  // The Google tileset while the photoreal view is shown, else null.
  getGoogle: () => (google && google.show ? google : null),
  tourDetail: () => (tourQ && google ? google.maximumScreenSpaceError : null),
};
