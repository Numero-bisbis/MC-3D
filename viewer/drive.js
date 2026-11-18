// Drive: take a car round the film set yourself, open-world style.
//
// Arrow keys or WASD to drive, Space for the handbrake, C to change the
// camera, Esc to leave. On a phone: the pedals on the right, steering on
// the left. The car follows the ground the set is built on and stops
// against buildings and the sea, using the drive map written by
// pipeline/build_filmset.py (viewer/filmset/monte-carlo-drive.png: height
// and what blocks a car, 1 m grid). Open it with "Drive" in Tours, or
// ?tour=drive.
/* global Cesium */
(function () {
  const M = window.MC3D;
  if (!M || !M.tours || !M.filmset) return;
  const { viewer } = M;
  const scene = viewer.scene, cam = viewer.camera, ctrl = scene.screenSpaceCameraController;
  const BASE = "filmset/";
  const MOBILE = window.matchMedia("(pointer: coarse)").matches || window.innerWidth < 700;

  // ---- The drive map ----------------------------------------------------
  let map = null, frame = null, inv = null;
  async function loadMap() {
    if (map) return map;
    const meta = await Cesium.Resource.fetchJson({ url: `${BASE}monte-carlo.json` });
    const d = meta.drive;
    const img = new Image();
    img.src = BASE + d.file;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = c.height = d.size;
    const g = c.getContext("2d", { willReadFrequently: true });
    g.drawImage(img, 0, 0);
    const px = g.getImageData(0, 0, d.size, d.size).data;
    const n = d.size, h = new Float32Array(n * n), f = new Uint8Array(n * n);
    for (let i = 0; i < n * n; i++) { h[i] = (px[i * 4] * 256 + px[i * 4 + 1]) / 100 - 10; f[i] = px[i * 4 + 2]; }
    const A = d.affine;                       // [dx, dy, 1] @ A = east, north, up
    const det = A[0][0] * A[1][1] - A[0][1] * A[1][0];
    map = { n, res: d.res, h, f, A, det, img };
    frame = Cesium.Transforms.eastNorthUpToFixedFrame(Cesium.Cartesian3.fromDegrees(meta.origin.lon, meta.origin.lat, meta.origin.height));
    inv = Cesium.Matrix4.inverseTransformation(frame, new Cesium.Matrix4());
    return map;
  }
  // East/north (m, around the set's origin) -> grid column/row.
  function toGrid(e, nn) {
    const A = map.A, ex = e - A[2][0], ny = nn - A[2][1];
    const dx = (ex * A[1][1] - ny * A[1][0]) / map.det, dy = (ny * A[0][0] - ex * A[0][1]) / map.det;
    return [dx / map.res, (map.n - dy) / map.res, dx, dy];
  }
  function sample(e, nn) {
    const [c, r, dx, dy] = toGrid(e, nn);
    const n = map.n;
    if (c < 1 || r < 1 || c > n - 2 || r > n - 2) return { z: 0, flag: 2 };
    const c0 = Math.floor(c - 0.5), r0 = Math.floor(r - 0.5), fc = c - 0.5 - c0, fr = r - 0.5 - r0;
    const H = map.h, i = r0 * n + c0;
    const hh = (H[i] * (1 - fc) + H[i + 1] * fc) * (1 - fr) + (H[i + n] * (1 - fc) + H[i + n + 1] * fc) * fr;
    const flag = map.f[Math.floor(r) * n + Math.floor(c)];
    const A = map.A;
    return { z: dx * A[0][2] + dy * A[1][2] + A[2][2] + hh, flag };
  }
  const blocked = (e, n) => { const f = sample(e, n).flag; return f === 1 || f === 2; };

  // ---- The car: a little Riviera sports coupé ---------------------------
  const RED = Cesium.Color.fromCssColorString("#c8102e");
  function part(geom, at, col, rot) {
    let m = Cesium.Matrix4.fromTranslation(new Cesium.Cartesian3(...at));
    if (rot) m = Cesium.Matrix4.multiply(m, Cesium.Matrix4.fromRotation(rot), new Cesium.Matrix4());
    return new Cesium.GeometryInstance({
      geometry: geom, modelMatrix: m,
      attributes: { color: Cesium.ColorGeometryInstanceAttribute.fromColor(col) },
    });
  }
  const box = (at, size, col) => part(Cesium.BoxGeometry.fromDimensions({
    dimensions: new Cesium.Cartesian3(...size), vertexFormat: Cesium.PerInstanceColorAppearance.VERTEX_FORMAT }), at, col);
  function makeCar() {
    const glass = Cesium.Color.fromCssColorString("#1d2a33"), dark = Cesium.Color.fromCssColorString("#151515");
    const chrome = Cesium.Color.fromCssColorString("#c9c9c9");
    const parts = [
      box([0, 0, 0.55], [4.3, 1.85, 0.6], RED),                  // body
      box([1.55, 0, 0.82], [1.2, 1.8, 0.12], RED),               // bonnet
      box([-0.25, 0, 1.08], [2.0, 1.55, 0.5], glass),            // cabin glass
      box([-0.3, 0, 1.35], [1.6, 1.5, 0.06], RED),               // roof
      box([-2.1, 0, 0.78], [0.25, 1.86, 0.12], dark),            // spoiler lip
      box([2.16, 0, 0.45], [0.06, 1.5, 0.22], dark),             // grille
      box([2.16, 0.68, 0.62], [0.06, 0.32, 0.12], Cesium.Color.fromCssColorString("#fff6d8")),
      box([2.16, -0.68, 0.62], [0.06, 0.32, 0.12], Cesium.Color.fromCssColorString("#fff6d8")),
      box([-2.16, 0.68, 0.66], [0.06, 0.36, 0.1], Cesium.Color.fromCssColorString("#ff2a1a")),
      box([-2.16, -0.68, 0.66], [0.06, 0.36, 0.1], Cesium.Color.fromCssColorString("#ff2a1a")),
      box([0, 0.93, 0.5], [3.0, 0.04, 0.08], chrome),
      box([0, -0.93, 0.5], [3.0, 0.04, 0.08], chrome),
    ];
    const wheel = Cesium.CylinderGeometry.createGeometry(new Cesium.CylinderGeometry({
      length: 0.26, topRadius: 0.36, bottomRadius: 0.36, slices: 18, vertexFormat: Cesium.PerInstanceColorAppearance.VERTEX_FORMAT }));
    const rim = Cesium.CylinderGeometry.createGeometry(new Cesium.CylinderGeometry({
      length: 0.28, topRadius: 0.22, bottomRadius: 0.22, slices: 12, vertexFormat: Cesium.PerInstanceColorAppearance.VERTEX_FORMAT }));
    const turn = Cesium.Matrix3.fromRotationX(Math.PI / 2);
    for (const x of [1.35, -1.35]) for (const y of [0.86, -0.86]) {
      parts.push(part(wheel, [x, y, 0.36], dark, turn));
      parts.push(part(rim, [x, y, 0.36], chrome, turn));
    }
    const body = scene.primitives.add(new Cesium.Primitive({
      geometryInstances: parts,
      appearance: new Cesium.PerInstanceColorAppearance({ flat: false, translucent: false }),
      asynchronous: false, show: false, shadows: Cesium.ShadowMode.CAST_ONLY,
    }));
    const shadow = scene.primitives.add(new Cesium.Primitive({
      geometryInstances: new Cesium.GeometryInstance({
        geometry: new Cesium.EllipsoidGeometry({ radii: new Cesium.Cartesian3(2.4, 1.05, 0.02), vertexFormat: Cesium.PerInstanceColorAppearance.VERTEX_FORMAT }),
        attributes: { color: Cesium.ColorGeometryInstanceAttribute.fromColor(Cesium.Color.BLACK.withAlpha(0.3)) },
      }),
      appearance: new Cesium.PerInstanceColorAppearance({ flat: true, translucent: true }),
      asynchronous: false, show: false, shadows: Cesium.ShadowMode.DISABLED,
    }));
    return { body, shadow };
  }
  let car = null;

  // ---- Places you pass ----------------------------------------------------
  const PLACES = [
    ["Place du Casino", 7.42796, 43.73917, 70],
    ["Hôtel de Paris", 7.42751, 43.73898, 45],
    ["Café de Paris", 7.42872, 43.73924, 40],
    ["Jardins des Boulingrins", 7.42788, 43.74025, 70],
    ["Fairmont hairpin", 7.42989, 43.74042, 45],
    ["The tunnel", 7.43005, 43.73935, 60],
    ["Port Hercule", 7.42560, 43.73580, 120],
    ["Avenue de Monte-Carlo", 7.42660, 43.73810, 50],
    ["Hôtel Hermitage", 7.42595, 43.73846, 45],
    ["Beausoleil", 7.42480, 43.74300, 140],
  ];
  let placesENU = null;
  function placesLocal() {
    const t = new Cesium.Cartesian3();
    return PLACES.map(([name, lon, lat, r]) => {
      Cesium.Matrix4.multiplyByPoint(inv, Cesium.Cartesian3.fromDegrees(lon, lat, 50), t);
      return { name, e: t.x, n: t.y, r };
    });
  }

  // ---- HUD ----------------------------------------------------------------
  const el = (tag, cls, parent, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    (parent || document.body).appendChild(e);
    return e;
  };
  const hud = el("div", "drive-hud");
  hud.hidden = true;
  const mini = el("canvas", "drive-map", hud);
  mini.width = mini.height = 300;
  const speedo = el("div", "drive-speed", hud);
  const speedN = el("span", "", speedo, "0");
  el("small", "", speedo, "km/h");
  const place = el("div", "drive-place", hud);
  const help = el("div", "drive-help", hud, MOBILE ? "" : "Arrows or WASD to drive · Space handbrake · C camera · Esc to leave");
  const exitBtn = el("button", "drive-exit", hud, "✕");
  exitBtn.setAttribute("aria-label", "Stop driving");
  exitBtn.onclick = () => M.tours.stop();
  const camBtn = el("button", "drive-cam", hud, "◎");
  camBtn.setAttribute("aria-label", "Change camera");
  camBtn.onclick = () => { view = (view + 1) % VIEWS.length; };
  const pads = el("div", "drive-pads", hud);
  const padL = el("div", "drive-pad left", pads), padR = el("div", "drive-pad right", pads);
  const bLeft = el("button", "", padL, "◀"), bRight = el("button", "", padL, "▶");
  const bBrake = el("button", "brake", padR, "▼"), bGas = el("button", "gas", padR, "▲");
  pads.hidden = !MOBILE;

  const keys = {};
  const touch = { left: false, right: false, gas: false, brake: false };
  for (const [b, k] of [[bLeft, "left"], [bRight, "right"], [bGas, "gas"], [bBrake, "brake"]]) {
    b.addEventListener("pointerdown", (e) => { e.preventDefault(); touch[k] = true; b.classList.add("on"); });
    for (const ev of ["pointerup", "pointercancel", "pointerleave"]) b.addEventListener(ev, () => { touch[k] = false; b.classList.remove("on"); });
  }
  window.addEventListener("keydown", (e) => {
    if (!on) return;
    keys[e.key.toLowerCase()] = true;
    if (e.key.startsWith("Arrow") || e.key === " ") e.preventDefault();
    if (e.key.toLowerCase() === "c") view = (view + 1) % VIEWS.length;
    if (e.key === "Escape") M.tours.stop();
  });
  window.addEventListener("keyup", (e) => { keys[e.key.toLowerCase()] = false; });
  const k = (...names) => names.some((n) => keys[n]);

  // Minimap: the drive map painted once (buildings, sea, ground, circuit),
  // then drawn turned with the car, which stays in the middle.
  let mapCanvas = null;
  function paintMap() {
    const n = map.n, c = document.createElement("canvas");
    c.width = c.height = n;
    const g = c.getContext("2d"), im = g.createImageData(n, n);
    for (let i = 0; i < n * n; i++) {
      const f = map.f[i];
      const col = f === 1 ? [70, 76, 86] : f === 2 ? [38, 92, 128] : f === 3 ? [235, 190, 60] : [196, 190, 172];
      im.data.set([...col, 255], i * 4);
    }
    g.putImageData(im, 0, 0);
    const R = window.MC3D_LAP_ROUTE;
    if (R) {
      g.strokeStyle = "rgba(220, 40, 40, .9)";
      g.lineWidth = 4;
      g.beginPath();
      const t = new Cesium.Cartesian3();
      R.points.forEach(([lon, lat], i) => {
        Cesium.Matrix4.multiplyByPoint(inv, Cesium.Cartesian3.fromDegrees(lon, lat, 50), t);
        const [cc, rr] = toGrid(t.x, t.y);
        if (i) g.lineTo(cc, rr); else g.moveTo(cc, rr);
      });
      g.stroke();
    }
    mapCanvas = c;
  }
  function drawMini() {
    const g = mini.getContext("2d"), W = mini.width;
    const [c, r] = toGrid(S.e, S.n);
    g.save();
    g.clearRect(0, 0, W, W);
    g.beginPath(); g.arc(W / 2, W / 2, W / 2 - 4, 0, Math.PI * 2); g.clip();
    g.fillStyle = "#26506d"; g.fillRect(0, 0, W, W);
    g.translate(W / 2, W / 2);
    // The grid's columns run (almost) east and rows south; turn so the
    // car's heading points up.
    g.rotate(S.yaw - Math.PI / 2 - Math.atan2(map.A[0][1], map.A[0][0]));
    const scale = 0.9;                        // px per metre
    g.scale(scale, scale);
    g.drawImage(mapCanvas, -c, -r);
    g.restore();
    g.fillStyle = "#fff"; g.strokeStyle = "#111"; g.lineWidth = 2;
    g.beginPath(); g.moveTo(W / 2, W / 2 - 11); g.lineTo(W / 2 + 8, W / 2 + 9); g.lineTo(W / 2, W / 2 + 4); g.lineTo(W / 2 - 8, W / 2 + 9); g.closePath();
    g.fill(); g.stroke();
  }

  // ---- Driving --------------------------------------------------------------
  const S = { e: 0, n: 0, z: 0, yaw: 0, v: 0, pitch: 0, roll: 0, steer: 0 };
  const VIEWS = [{ back: 8.5, up: 3.2, look: 1.4 }, { back: 15, up: 6.5, look: 1.0 }, { back: -0.2, up: 1.15, look: 1.1 }];
  let view = 0, on = false, last = 0, camYaw = 0, lastPlace = "", placeTimer = 0, oldMinZoom = null, remove = null;
  const MAXV = 50, MAXR = -9;

  function spawn(at) {
    // On the Grand Prix circuit in Casino square, pointing along it.
    const t = new Cesium.Cartesian3(), u = new Cesium.Cartesian3();
    const R = window.MC3D_LAP_ROUTE;
    let best = 1e9, bi = 0;
    R.points.forEach(([lon, lat], i) => {
      const d = (lon - 7.42815) ** 2 + (lat - 43.73935) ** 2;
      if (d < best) { best = d; bi = i; }
    });
    if (at !== undefined) bi = at;
    const [lo, la] = R.points[bi], [lo2, la2] = R.points[(bi + 3) % R.points.length];
    Cesium.Matrix4.multiplyByPoint(inv, Cesium.Cartesian3.fromDegrees(lo, la, 50), t);
    Cesium.Matrix4.multiplyByPoint(inv, Cesium.Cartesian3.fromDegrees(lo2, la2, 50), u);
    Object.assign(S, { e: t.x, n: t.y, yaw: Math.atan2(u.y - t.y, u.x - t.x), v: 0, steer: 0 });
    S.z = sample(S.e, S.n).z;
    camYaw = S.yaw;
  }

  function step(dt) {
    const gas = k("arrowup", "w", "z") || touch.gas, brake = k("arrowdown", "s") || touch.brake;
    const left = k("arrowleft", "a", "q") || touch.left, right = k("arrowright", "d") || touch.right;
    const hand = k(" ");
    // Throttle, brakes, reverse, rolling drag.
    if (gas) S.v += (S.v < 0 ? 14 : 7.5 * (1 - S.v / MAXV)) * dt;
    else if (brake) S.v -= (S.v > 0.5 ? 14 : 4) * dt;
    else S.v -= Math.sign(S.v) * Math.min(Math.abs(S.v), (0.6 + 0.012 * S.v * S.v) * dt);
    if (hand) S.v -= Math.sign(S.v) * Math.min(Math.abs(S.v), 9 * dt);
    S.v = Math.max(MAXR, Math.min(MAXV, S.v));
    // Steering: quick at low speed, calmer when fast.
    const want = (left ? 1 : 0) - (right ? 1 : 0);
    S.steer += (want - S.steer) * Math.min(1, dt * 7);
    const grip = hand ? 1.6 : 1;
    const yawRate = S.steer * grip * Math.min(1, Math.abs(S.v) / 4) * (1.25 / (1 + Math.abs(S.v) / 22)) * Math.sign(S.v || 1);
    S.yaw += yawRate * dt;
    // Move, then check the corners of the car against buildings and sea.
    const ce = Math.cos(S.yaw), sn = Math.sin(S.yaw);
    const ne = S.e + ce * S.v * dt, nn = S.n + sn * S.v * dt;
    const ahead = Math.sign(S.v || 1) * 2.2;
    const hit = [[ahead, 0.9], [ahead, -0.9], [ahead, 0]].some(([a, b]) => blocked(ne + ce * a - sn * b, nn + sn * a + ce * b));
    const here = sample(ne, nn);
    const climb = here.z - S.z;
    if (hit || climb > 1.2) {
      S.v = -S.v * 0.25;                       // bump off the wall
      shake = Math.min(1, Math.abs(S.v) / 6 + 0.2);
    } else {
      S.e = ne; S.n = nn;
    }
    // Sit on the ground: pitch and roll from the height under the wheels.
    const f = sample(S.e + ce * 1.4, S.n + sn * 1.4).z, b = sample(S.e - ce * 1.4, S.n - sn * 1.4).z;
    const l = sample(S.e - sn * 0.85, S.n + ce * 0.85).z, r = sample(S.e + sn * 0.85, S.n - ce * 0.85).z;
    const zg = (f + b + l + r) / 4;
    S.z += (zg - S.z) * Math.min(1, dt * 18);
    S.pitch = Math.atan2(f - b, 2.8);
    S.roll = Math.atan2(l - r, 1.7);
  }
  let shake = 0;

  const scratch = { m: new Cesium.Matrix4(), q: new Cesium.Quaternion(), p: new Cesium.Cartesian3(), hpr: new Cesium.HeadingPitchRoll() };
  function local(e, n, z, out) { return Cesium.Matrix4.multiplyByPoint(frame, Cesium.Cartesian3.fromElements(e, n, z, scratch.p), out || new Cesium.Cartesian3()); }
  function place3d() {
    // Car frame in ENU: yaw about up, then pitch and roll.
    const rot = Cesium.Matrix3.multiply(Cesium.Matrix3.fromRotationZ(S.yaw),
      Cesium.Matrix3.multiply(Cesium.Matrix3.fromRotationY(-S.pitch), Cesium.Matrix3.fromRotationX(S.roll), new Cesium.Matrix3()), new Cesium.Matrix3());
    const m = Cesium.Matrix4.fromRotationTranslation(rot, new Cesium.Cartesian3(S.e, S.n, S.z + 0.02));
    car.body.modelMatrix = Cesium.Matrix4.multiply(frame, m, car.body.modelMatrix);
    car.shadow.modelMatrix = Cesium.Matrix4.multiply(frame,
      Cesium.Matrix4.fromRotationTranslation(Cesium.Matrix3.fromRotationZ(S.yaw), new Cesium.Cartesian3(S.e, S.n, S.z + 0.04)), car.shadow.modelMatrix);
    // Chase camera: swings round behind the car with a little lag.
    const V = VIEWS[view];
    camYaw += Cesium.Math.negativePiToPi(S.yaw - camYaw) * Math.min(1, 0.06 + (view === 2 ? 1 : 0));
    const cy = Math.cos(camYaw), sy = Math.sin(camYaw);
    const jitter = shake * 0.15 * Math.sin(performance.now() / 23);
    shake *= 0.9;
    let ce = S.e - cy * V.back, cn = S.n - sy * V.back;
    let cz = S.z + V.up + jitter;
    // Keep the camera out of buildings and above the ground.
    if (view !== 2) {
      for (let i = 0; i < 6 && blocked(ce, cn); i++) { ce += cy * V.back / 8; cn += sy * V.back / 8; cz += 0.6; }
      cz = Math.max(cz, sample(ce, cn).z + 1.2);
    }
    const pos = local(ce, cn, cz);
    const tgt = local(S.e + cy * (view === 2 ? 20 : 6), S.n + sy * (view === 2 ? 20 : 6), S.z + V.look + (view === 2 ? 0 : 0));
    const dir = Cesium.Cartesian3.normalize(Cesium.Cartesian3.subtract(tgt, pos, new Cesium.Cartesian3()), new Cesium.Cartesian3());
    const upW = Cesium.Matrix4.multiplyByPointAsVector(frame, Cesium.Cartesian3.UNIT_Z, new Cesium.Cartesian3());
    const right = Cesium.Cartesian3.normalize(Cesium.Cartesian3.cross(dir, upW, new Cesium.Cartesian3()), new Cesium.Cartesian3());
    const up = Cesium.Cartesian3.cross(right, dir, new Cesium.Cartesian3());
    cam.setView({ destination: pos, orientation: { direction: dir, up } });
    car.body.show = car.shadow.show = view !== 2;
  }

  function tick() {
    if (!on) return;
    const now = performance.now();
    const dt = Math.min(0.05, (now - last) / 1000 || 0);
    last = now;
    // Small fixed steps keep the bumps steady at low frame rates.
    for (let t = dt; t > 1e-4; t -= 1 / 60) step(Math.min(t, 1 / 60));
    place3d();
    speedN.textContent = Math.round(Math.abs(S.v) * 3.6);
    drawMini();
    // Name the place you are passing, once each time you arrive.
    let near = "";
    for (const p of placesENU) if (Math.hypot(p.e - S.e, p.n - S.n) < p.r) { near = p.name; break; }
    if (sample(S.e, S.n).flag === 3) near = "The tunnel";
    if (near && near !== lastPlace) {
      place.textContent = near;
      place.classList.remove("in"); void place.offsetWidth; place.classList.add("in");
      clearTimeout(placeTimer);
      placeTimer = setTimeout(() => place.classList.remove("in"), 3500);
    }
    if (near) lastPlace = near;
    else if (lastPlace && placesENU.every((p) => Math.hypot(p.e - S.e, p.n - S.n) > p.r * 1.6)) lastPlace = "";
  }

  async function start() {
    on = true;
    hud.hidden = false;
    document.body.classList.add("driving");
    if (!M.filmset.isOn()) await M.filmset.setFilm(true);
    await loadMap();
    if (!on) return;
    if (!mapCanvas) paintMap();
    placesENU = placesENU || placesLocal();
    car = car || makeCar();
    spawn();
    oldMinZoom = ctrl.minimumZoomDistance;
    ctrl.enableInputs = false;
    ctrl.minimumZoomDistance = 1;
    if (M.legends && M.legends.setSky) M.legends.setSky(true);
    last = performance.now();
    remove = scene.preUpdate.addEventListener(tick);
    help.classList.add("in");
    setTimeout(() => help.classList.remove("in"), 6000);
  }
  function stop() {
    on = false;
    hud.hidden = true;
    document.body.classList.remove("driving");
    if (remove) { remove(); remove = null; }
    if (car) car.body.show = car.shadow.show = false;
    ctrl.enableInputs = true;
    if (oldMinZoom !== null) { ctrl.minimumZoomDistance = oldMinZoom; oldMinZoom = null; }
    if (M.legends && M.legends.setSky) M.legends.setSky(false);
    for (const key in keys) keys[key] = false;
    if (frame) {
      const p = local(S.e - Math.cos(S.yaw) * 120, S.n - Math.sin(S.yaw) * 120, S.z + 90);
      cam.flyTo({ destination: p, orientation: { heading: Cesium.Math.PI_OVER_TWO - S.yaw, pitch: Cesium.Math.toRadians(-30), roll: 0 }, duration: 2 });
    }
  }

  M.tours.register({ id: "drive", label: "Drive", start, stop });
  M.drive = { get state() { return { ...S }; }, spawnAt: (i) => spawn(i), sample: (e, n) => (map ? sample(e, n) : null), set view(v) { view = v; } };
})();
