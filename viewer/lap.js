// "Grand Prix lap": one lap of the Circuit de Monaco, flown low along the
// real racing route, with the corner names coming up as the camera passes.
//
// Everything about the flight is worked out once, as a function of the
// distance along the lap (camera height, where it looks, how fast it goes),
// so the lap can be paused, resumed and scrubbed freely.
/* global Cesium */
(function () {
  const M = window.MC3D, R = window.MC3D_LAP_ROUTE;
  if (!M || !M.tours || !R) return;
  const { viewer } = M;
  const scene = viewer.scene, cam = viewer.camera;
  const hud = M.tours.hud;

  // Where each corner is (metres from the start line) and what to say.
  const CORNERS = [
    { s: 0, k: "Start / finish", t: "Boulevard Albert Iᵉʳ", n: "The grid, in the middle of the city" },
    { s: 195, k: "Turn 1", t: "Sainte-Dévote", n: "Named after the chapel of Monaco's patron saint" },
    { s: 330, k: "Turn 2", t: "Beau Rivage", n: "The climb towards the Casino" },
    { s: 795, k: "Turn 3", t: "Massenet", n: "A long left-hander around the Opera" },
    { s: 895, k: "Turn 4", t: "Casino", n: "Place du Casino, the heart of Monte-Carlo" },
    { s: 1100, k: "Turn 5", t: "Mirabeau", n: "Downhill, back towards the sea" },
    { s: 1305, k: "Turn 6", t: "Grand Hotel Hairpin", n: "The slowest corner in Formula 1" },
    { s: 1455, k: "Turn 8", t: "Portier", n: "Onto the seafront" },
    { s: 1560, k: "Turn 9", t: "The Tunnel", n: "Under the hotel, the fastest part of the lap" },
    { s: 2090, k: "Turns 10 – 11", t: "Nouvelle Chicane", n: "Back into daylight at Port Hercule" },
    { s: 2385, k: "Turn 12", t: "Tabac", n: "Named after a tobacconist's kiosk on the quay" },
    { s: 2600, k: "Turns 13 – 16", t: "Piscine", n: "Around the Rainier III swimming pool" },
    { s: 2925, k: "Turn 18", t: "La Rascasse", n: "A tight turn around the famous harbour restaurant" },
    { s: 3005, k: "Turn 19", t: "Anthony Noghès", n: "The last corner, named after the founder of the race" },
  ];
  const LEAD = 75;            // m before a corner at which its name comes up

  // Two ways of flying the lap.
  // "air": a helicopter following the cars, about 130 m up. Used with the
  //   open data, which is made from aerial scans and looks right from there.
  // "street": a few metres above the road, diving through the tunnel. Used
  //   with the photoreal layer, which holds up at street level.
  // Add ?lap=street or ?lap=air to the address to force one.
  const STYLES = {
    air: {
      height: 110,          // m above the road
      look: 150,            // m: the camera turns towards the road this far ahead
      // Looking down by this many degrees keeps the skyline in the top of
      // the picture, on a phone held upright or sideways.
      pitchUpright: -23, pitchWide: -18,
      pathSmooth: 12, dirSmooth: 14, heightSmooth: 40,   // in 2 m steps
      vMax: 40, vTunnel: 40, vMin: 12,                   // m/s
      turnRate: 0.42,       // rad/s, how fast the view may swing in corners
      lean: 0.03, fovUpright: 60, fovWide: 80, dive: false,
    },
    street: {
      height: 7, over: 2.5, tunnelHeight: 3.5, look: 46,
      pathSmooth: 0, dirSmooth: 6, heightSmooth: 11,
      vMax: 30, vTunnel: 62, vMin: 9,
      turnRate: 0.52, lean: 0.085, fovUpright: 84, fovWide: 68, dive: true,
    },
  };
  const ACCEL = 7;          // m/s², gentle speeding up and slowing down

  // ---- Route geometry, in metres around the start line -------------------
  const P = R.points, N = P.length, DS = R.step, LEN = R.length;
  const wrap = (i) => ((i % N) + N) % N;
  const origin = Cesium.Cartesian3.fromDegrees(P[0][0], P[0][1], 0);
  const toWorld = Cesium.Transforms.eastNorthUpToFixedFrame(origin);
  const toLocal = Cesium.Matrix4.inverseTransformation(toWorld, new Cesium.Matrix4());
  const X = new Float64Array(N), Y = new Float64Array(N), ZG = new Float64Array(N), ZC = new Float64Array(N);
  const tmp = new Cesium.Cartesian3();
  for (let i = 0; i < N; i++) {
    Cesium.Matrix4.multiplyByPoint(toLocal, Cesium.Cartesian3.fromDegrees(P[i][0], P[i][1], P[i][2]), tmp);
    X[i] = tmp.x; Y[i] = tmp.y; ZG[i] = tmp.z; ZC[i] = tmp.z + (P[i][3] - P[i][2]);
  }
  function smooth(a, sigma) {       // Gaussian along the closed lap
    if (!sigma) return a;
    const r = Math.ceil(sigma * 3), k = [], out = new Float64Array(N);
    let sum = 0;
    for (let j = -r; j <= r; j++) { const w = Math.exp(-0.5 * (j / sigma) ** 2); k.push(w); sum += w; }
    for (let i = 0; i < N; i++) {
      let v = 0;
      for (let j = -r; j <= r; j++) v += a[wrap(i + j)] * k[j + r];
      out[i] = v / sum;
    }
    return out;
  }
  const smoothstep = (a, b, x) => { const t = Math.min(1, Math.max(0, (x - a) / (b - a))); return t * t * (3 - 2 * t); };
  const TUN = R.tunnels.map(([a, b]) => [a, b]);
  // 1 inside a tunnel, easing to 0 over `before` / `after` metres outside.
  function tunnelWeight(s, before, after) {
    let w = 0;
    for (const [a, b] of TUN) w = Math.max(w, smoothstep(a - before, a, s) * (1 - smoothstep(b, b + after, s)));
    return w;
  }

  // ---- Bake one flight ------------------------------------------------
  const baked = {};
  function bake(name) {
    if (baked[name]) return baked[name];
    const S = STYLES[name];
    const CX = smooth(X, S.pathSmooth), CY = smooth(Y, S.pathSmooth);

    // Camera height.
    let ZCAM;
    if (S.dive) {
      // Low over the road, but above whatever stands over it a little behind
      // and some way ahead, eased so the climb is never abrupt.
      const need = new Float64Array(N);
      for (let i = 0; i < N; i++) {
        let top = -1e9;
        for (let j = -6; j <= 22; j++) top = Math.max(top, ZC[wrap(i + j)]);
        need[i] = Math.max(ZG[i] + S.height, top + S.over);
      }
      ZCAM = smooth(need, S.heightSmooth);
      for (let pass = 0; pass < 3; pass++) {
        const lifted = new Float64Array(N);
        for (let i = 0; i < N; i++) lifted[i] = Math.max(ZCAM[i], need[i]);
        ZCAM = smooth(lifted, 7);
      }
      for (let i = 0; i < N; i++) {
        const w = tunnelWeight(i * DS, 70, 55);
        ZCAM[i] = ZCAM[i] * (1 - w) + (ZG[i] + S.tunnelHeight) * w;
      }
    } else {
      ZCAM = smooth(ZG, S.heightSmooth).map((z) => z + S.height);
    }

    // View direction: towards the road ahead, smoothed.
    let DX = new Float64Array(N), DY = new Float64Array(N), DZ = new Float64Array(N);
    const nLook = Math.round(S.look / DS);
    for (let i = 0; i < N; i++) {
      const j = wrap(i + nLook);
      const dx = CX[j] - CX[i], dy = CY[j] - CY[i];
      const dz = S.dive ? (ZG[j] + 2 + 0.3 * (ZCAM[i] - ZG[i])) - ZCAM[i] : 0;
      const l = Math.hypot(dx, dy, dz);
      DX[i] = dx / l; DY[i] = dy / l; DZ[i] = dz / l;
    }
    DX = smooth(DX, S.dirSmooth); DY = smooth(DY, S.dirSmooth); DZ = smooth(DZ, S.dirSmooth * 1.5);

    // Speed: quick on the straights, slow enough in corners that the view
    // never swings faster than turnRate.
    const KAPPA = new Float64Array(N);
    for (let i = 0; i < N; i++) {
      const a = Math.atan2(DX[wrap(i - 1)], DY[wrap(i - 1)]), b = Math.atan2(DX[wrap(i + 1)], DY[wrap(i + 1)]);
      KAPPA[i] = Cesium.Math.negativePiToPi(b - a) / (2 * DS);     // + = turning right
    }
    const V = new Float64Array(N);
    for (let i = 0; i < N; i++) {
      const top = S.vMax + (S.vTunnel - S.vMax) * tunnelWeight(i * DS, 40, 60);
      V[i] = Math.max(S.vMin, Math.min(top, S.turnRate / Math.max(1e-6, Math.abs(KAPPA[i]))));
    }
    V[0] = Math.min(V[0], 5);                                       // a standing start
    for (let i = 1; i < N; i++) V[i] = Math.min(V[i], Math.sqrt(V[i - 1] ** 2 + 2 * ACCEL * DS));
    for (let i = N - 2; i >= 0; i--) V[i] = Math.min(V[i], Math.sqrt(V[i + 1] ** 2 + 2 * ACCEL * DS));
    const T = new Float64Array(N + 1);                              // time at each point
    for (let i = 0; i < N; i++) T[i + 1] = T[i] + DS / ((V[i] + V[wrap(i + 1)]) / 2);
    // A slight lean into the corners.
    let ROLL = new Float64Array(N);
    for (let i = 0; i < N; i++) ROLL[i] = Cesium.Math.clamp(KAPPA[i] * V[i] * 0.2, -S.lean, S.lean);
    ROLL = smooth(ROLL, 8);
    return (baked[name] = { S, CX, CY, ZCAM, DX, DY, DZ, ROLL, T, DURATION: T[N] });
  }
  let F = bake("air");        // the flight in use

  // ---- Camera pose at any moment --------------------------------------
  const lerp = (A, i, f) => A[wrap(i)] * (1 - f) + A[wrap(i + 1)] * f;
  function indexAt(t) {                 // fractional point index at time t
    const T = F.T;
    t = Math.min(F.DURATION, Math.max(0, t));
    let lo = 0, hi = N;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (T[mid] <= t) lo = mid; else hi = mid; }
    return lo + (t - T[lo]) / (T[lo + 1] - T[lo]);
  }
  const pose = { position: new Cesium.Cartesian3(), direction: new Cesium.Cartesian3(), up: new Cesium.Cartesian3() };
  const right = new Cesium.Cartesian3(), zUp = new Cesium.Cartesian3(0, 0, 1);
  function poseAt(u) {
    const i = Math.floor(u), f = u - i;
    const d = pose.direction, up = pose.up;
    Cesium.Cartesian3.fromElements(lerp(F.DX, i, f), lerp(F.DY, i, f), lerp(F.DZ, i, f), d);
    Cesium.Cartesian3.normalize(d, d);
    if (!F.S.dive) {            // level heading, tilted down by a set angle
      const c = scene.canvas;
      const p = Cesium.Math.toRadians(c.clientHeight > c.clientWidth ? F.S.pitchUpright : F.S.pitchWide);
      d.x *= Math.cos(p); d.y *= Math.cos(p); d.z = Math.sin(p);
    }
    Cesium.Cartesian3.cross(d, zUp, right);
    Cesium.Cartesian3.normalize(right, right);
    Cesium.Cartesian3.cross(right, d, up);
    const r = lerp(F.ROLL, i, f);
    up.x = up.x * Math.cos(r) + right.x * Math.sin(r);
    up.y = up.y * Math.cos(r) + right.y * Math.sin(r);
    up.z = up.z * Math.cos(r) + right.z * Math.sin(r);
    Cesium.Cartesian3.fromElements(lerp(F.CX, i, f), lerp(F.CY, i, f), lerp(F.ZCAM, i, f), pose.position);
    Cesium.Matrix4.multiplyByPoint(toWorld, pose.position, pose.position);
    Cesium.Matrix4.multiplyByPointAsVector(toWorld, d, d);
    Cesium.Matrix4.multiplyByPointAsVector(toWorld, up, up);
    return pose;
  }
  function applyPose(u) {
    const p = poseAt(u);
    cam.setView({ destination: p.position, orientation: { direction: p.direction, up: p.up } });
  }

  // ---- On-screen extras: mini map, start lights, tunnel ----------------
  const mk = (tag, cls, parent) => { const e = document.createElement(tag); e.className = cls; parent.appendChild(e); return e; };
  const tunnel = mk("div", "lap-tunnel", hud.root);
  hud.root.insertBefore(tunnel, hud.root.firstChild);     // under the caption and the bar
  for (let i = 0; i < 3; i++) mk("i", "", tunnel);
  const lights = mk("div", "lap-lights", hud.root);
  for (let i = 0; i < 5; i++) mk("i", "", lights);
  const map = mk("div", "lap-map", hud.root);
  let minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;
  for (let i = 0; i < N; i++) { minX = Math.min(minX, X[i]); maxX = Math.max(maxX, X[i]); minY = Math.min(minY, Y[i]); maxY = Math.max(maxY, Y[i]); }
  const mx = (i) => (X[i] - minX).toFixed(0), my = (i) => (maxY - Y[i]).toFixed(0);
  let dPath = "";
  for (let i = 0; i < N; i += 4) dPath += `${i ? "L" : "M"}${mx(i)} ${my(i)}`;
  dPath += "Z";
  const pad = 40;
  map.innerHTML = `<svg viewBox="${-pad} ${-pad} ${maxX - minX + 2 * pad} ${maxY - minY + 2 * pad}" aria-hidden="true">
    <path class="all" d="${dPath}" style="stroke-width:26"/>
    <path class="done" d="${dPath}" pathLength="1" style="stroke-width:30" stroke-dasharray="0 1"/>
    <circle class="car" r="34" style="stroke-width:20"/></svg>`;
  const mapDone = map.querySelector(".done"), mapCar = map.querySelector(".car");
  for (const e of [tunnel, lights, map]) e.hidden = true;

  // The racing route drawn on the ground (not through the tunnel).
  const lines = [];
  let tunnelLine = null;
  function buildLines() {
    if (lines.length) return;
    const clamp = Cesium.Entity.supportsPolylinesOnTerrain(scene);
    let run = [];
    const flush = () => {
      if (run.length > 1) {
        lines.push(viewer.entities.add({
          show: false,
          polyline: {
            positions: run, width: 5, clampToGround: clamp,
            classificationType: Cesium.ClassificationType.BOTH,
            material: new Cesium.PolylineGlowMaterialProperty({
              glowPower: 0.28, taperPower: 1, color: Cesium.Color.fromCssColorString("#ffd27a").withAlpha(0.95),
            }),
          },
        }));
      }
      run = [];
    };
    const under = [];
    for (let i = 0; i <= N; i += 2) {
      const p = P[wrap(i)], c = Cesium.Cartesian3.fromDegrees(p[0], p[1], p[2] + 0.4);
      if (tunnelWeight(wrap(i) * DS, 0, 0) > 0.5) { if (!under.length && run.length) under.push(run[run.length - 1]); under.push(c); flush(); continue; }
      if (under.length && !run.length) under.push(c);
      run.push(c);
    }
    flush();
    // The tunnel, dashed over the hotel and gardens it runs beneath.
    if (under.length > 1) {
      tunnelLine = viewer.entities.add({
        show: false,
        polyline: {
          positions: under, width: 4, clampToGround: clamp, classificationType: Cesium.ClassificationType.BOTH,
          material: new Cesium.PolylineDashMaterialProperty({
            color: Cesium.Color.fromCssColorString("#ffd27a").withAlpha(0.9), dashLength: 14,
          }),
        },
      });
    }
  }
  const showLines = (on) => {
    lines.forEach((l) => { l.show = on; });
    if (tunnelLine) tunnelLine.show = on && !F.S.dive;
  };

  // ---- Playing ---------------------------------------------------------
  const ctrl = scene.screenSpaceCameraController;
  let state = "off";          // off | intro | playing | paused | ended
  let styleName = "air";
  let flying = false, oldMinZoom = null;
  let t = 0, last = 0, run = 0, looked = false, oldFov = null, timers = [];
  const later = (fn, ms) => { const id = setTimeout(fn, ms); timers.push(id); return id; };
  const clearTimers = () => { timers.forEach(clearTimeout); timers = []; };

  function cornerAt(s) {
    let c = CORNERS[0];
    for (const k of CORNERS) if (s >= k.s - LEAD) c = k;
    return c;
  }
  function draw() {
    const u = indexAt(t), s = u * DS;
    const c = cornerAt(s);
    hud.caption(c.k, c.t, c.n);
    hud.progress(s / LEN);
    mapDone.setAttribute("stroke-dasharray", `${(s / LEN).toFixed(4)} 1`);
    const i = wrap(Math.round(u));
    mapCar.setAttribute("cx", mx(i)); mapCar.setAttribute("cy", my(i));
    let dark = 0;
    if (F.S.dive) for (const [a, b] of TUN) dark = Math.max(dark, smoothstep(a - 26, a - 2, s) * (1 - smoothstep(b + 8, b + 46, s)));
    tunnel.style.opacity = dark.toFixed(3);
    return u;
  }
  function tick() {
    // Paused and not yet touched: hold the shot (Cesium would otherwise
    // nudge the camera while the ground is still loading).
    if (state === "paused" && !looked && !flying) applyPose(indexAt(t));
    // Cesium hands the gestures back after every glide; keep them off
    // whenever the lap, not the visitor, is steering.
    if ((state === "playing" || state === "intro" || flying) && ctrl.enableInputs) ctrl.enableInputs = false;
    if (state !== "playing") return;
    const now = performance.now();
    t += Math.min(0.1, (now - last) / 1000);
    last = now;
    if (t >= F.DURATION) { finish(); return; }
    applyPose(draw());
  }
  scene.preUpdate.addEventListener(tick);

  function setFov() {
    const f = cam.frustum;
    if (!(f instanceof Cesium.PerspectiveFrustum)) return;
    if (oldFov === null) oldFov = f.fov;
    // Cesium's angle is across the longer side of the screen. A phone held
    // upright needs a wide one, or the street feels like a keyhole.
    const c = scene.canvas, portrait = c.clientHeight > c.clientWidth;
    f.fov = Cesium.Math.toRadians(portrait ? F.S.fovUpright : F.S.fovWide);
  }
  window.addEventListener("resize", () => { if (state !== "off") setFov(); });

  function play() {
    clearTimers();
    lights.classList.remove("show");
    const go = () => {
      state = "playing";
      looked = false;
      last = performance.now();
      ctrl.enableInputs = false;
      tunnel.classList.remove("paused");
      hud.playing(true);
      hud.tip("");
    };
    if (looked) {             // the visitor looked around: glide back first
      const p = poseAt(indexAt(t)), mine = ++run;
      ctrl.enableInputs = false;
      flying = true;
      hud.playing(true);
      hud.tip("");
      cam.flyTo({
        destination: Cesium.Cartesian3.clone(p.position),
        orientation: { direction: Cesium.Cartesian3.clone(p.direction), up: Cesium.Cartesian3.clone(p.up) },
        duration: 0.9,
        complete: () => { flying = false; if (mine === run && state === "paused") go(); },
        cancel: () => { flying = false; },
      });
    } else go();
  }
  function pause(byTouch) {
    if (state !== "playing") return;
    state = "paused";
    ctrl.enableInputs = true;
    tunnel.classList.add("paused");
    hud.playing(false);
    hud.tip(byTouch ? "Paused · drag to look around · ▶ to continue" : "Paused · drag to look around");
  }
  function toggle() {
    if (state === "intro") { cam.cancelFlight(); run++; t = 0; applyPose(draw()); play(); }
    else if (state === "playing") pause(false);
    else if (state === "paused") play();
    else if (state === "ended") begin();
  }
  function seek(f) {
    if (state === "off") return;
    if (state === "intro" || state === "ended") { cam.cancelFlight(); run++; clearTimers(); lights.classList.remove("show"); showLines(true); state = "paused"; ctrl.enableInputs = true; hud.show(handlers); for (const e of [tunnel, map]) e.hidden = false; }
    // The bar shows distance, so jump by distance.
    const target = Math.min(N - 1, Math.max(0, f) * N), lo = Math.floor(target);
    t = Math.min(F.DURATION - 0.05, F.T[lo] + (target - lo) * (F.T[lo + 1] - F.T[lo]));
    looked = false;
    applyPose(draw());
  }

  // Touching the view while the lap runs pauses it, so you can look around.
  scene.canvas.addEventListener("pointerdown", () => {
    if (state === "playing") pause(true);
    if (state === "paused") looked = true;
  });

  // Street View at each corner: a point on the road just before it, looking
  // along the track towards the corner.
  const CORNER_SPOTS = CORNERS.map((c) => {
    const s0 = Math.max(0, c.s - 25), i = wrap(Math.round(s0 / DS)), j = wrap(i + Math.round(30 / DS));
    const [lng, lat] = P[i], [lng2, lat2] = P[j];
    const heading = Math.atan2((lng2 - lng) * Math.cos(lat * Math.PI / 180), lat2 - lat) * 180 / Math.PI;
    return { lat, lng, heading, kicker: c.k, title: c.t };
  });
  // Opens the panorama of the corner the lap is at (or coming up to); the
  // lap waits, paused, until ▶ is pressed again.
  function streetViewAt(s, pauseLap) {
    if (!M.streetView) return;
    if (pauseLap) pauseLap();
    M.streetView.open(CORNER_SPOTS, CORNERS.indexOf(cornerAt(s)));
  }
  const svButton = (getS, pauseLap) => ({
    html: M.streetView ? M.streetView.ICON : "",
    title: "Street View of this corner",
    onClick: () => streetViewAt(getS(), pauseLap),
  });

  const handlers = {
    onToggle: toggle, onSeek: seek, onReplay: () => begin(), marks: CORNERS.slice(1).map((c) => c.s / LEN),
    buttons: M.streetView && M.streetView.available
      ? [svButton(() => indexAt(t) * DS, () => (state === "intro" ? seek(0) : pause(false)))] : [],
  };

  function begin() {
    clearTimers();
    const mine = ++run;
    state = "intro";
    t = 0;
    looked = false;
    ctrl.enableInputs = false;
    const photoreal = document.body.classList.contains("photoreal");
    if (M.getMode() !== "surface" && !photoreal) M.setMode("surface");
    const asked = new URLSearchParams(location.search).get("lap");
    styleName = STYLES[asked] ? asked : (photoreal ? "street" : "air");
    F = bake(styleName);
    if (M.setTourQuality) M.setTourQuality(F.S.dive);   // steady detail at street level
    buildLines();
    showLines(true);
    setFov();
    // The usual "no closer than 60 m" rule would lift a paused street-level
    // camera into the air; it comes back when the tour closes.
    if (oldMinZoom === null) { oldMinZoom = ctrl.minimumZoomDistance; ctrl.minimumZoomDistance = 2; }
    hud.show(handlers);
    for (const e of [tunnel, lights, map]) e.hidden = false;
    tunnel.style.opacity = 0;
    lights.querySelectorAll("i").forEach((l) => l.classList.remove("on"));
    hud.caption("Grand Prix lap", "Circuit de Monaco", "3.337 km · 19 corners · raced since 1929");
    hud.progress(0);
    mapDone.setAttribute("stroke-dasharray", "0 1");
    mapCar.setAttribute("cx", mx(0)); mapCar.setAttribute("cy", my(0));
    const p = poseAt(0);
    cam.flyTo({
      destination: Cesium.Cartesian3.clone(p.position),
      orientation: { direction: Cesium.Cartesian3.clone(p.direction), up: Cesium.Cartesian3.clone(p.up) },
      duration: 4.5,
      easingFunction: Cesium.EasingFunction.CUBIC_IN_OUT,
      complete: () => { if (mine === run && state === "intro") startLights(mine); },
    });
  }
  // Five red lights come on one by one, then go out: go.
  function startLights(mine) {
    const dots = [...lights.querySelectorAll("i")];
    lights.classList.add("show");
    dots.forEach((d, i) => later(() => d.classList.add("on"), 350 + i * 420));
    later(() => {
      if (mine !== run || state !== "intro") return;
      dots.forEach((d) => d.classList.remove("on"));
      later(() => lights.classList.remove("show"), 500);
      draw();
      play();
    }, 350 + 5 * 420 + 650);
  }
  function finish() {
    state = "ended";
    run++;
    ctrl.enableInputs = false;
    tunnel.style.opacity = 0;
    hud.progress(1);
    // Rise to a view of the whole circuit, the route glowing on the ground.
    const c = scene.canvas, portrait = c.clientHeight > c.clientWidth;
    const centre = Cesium.Matrix4.multiplyByPoint(toWorld,
      new Cesium.Cartesian3((minX + maxX) / 2, (minY + maxY) / 2, ZG[0]), new Cesium.Cartesian3());
    cam.flyToBoundingSphere(new Cesium.BoundingSphere(centre, 1), {
      offset: new Cesium.HeadingPitchRange(Cesium.Math.toRadians(318), Cesium.Math.toRadians(-38), portrait ? 2300 : 1500),
      duration: 4,
      complete: () => { if (state === "ended") ctrl.enableInputs = true; },
    });
    later(() => { if (state === "ended") hud.finish("Lap complete", "Circuit de Monaco", "3.337 km through the streets of the Principality"); }, 1800);
  }
  function stop() {
    const was = state;
    if (was !== "off" && M.setTourQuality) M.setTourQuality(false);
    state = "off";
    run++;
    clearTimers();
    cam.cancelFlight();
    ctrl.enableInputs = true;
    flying = false;
    if (oldMinZoom !== null) { ctrl.minimumZoomDistance = oldMinZoom; oldMinZoom = null; }
    showLines(false);
    for (const e of [tunnel, lights, map]) e.hidden = true;
    lights.classList.remove("show");
    if (oldFov !== null && cam.frustum instanceof Cesium.PerspectiveFrustum) { cam.frustum.fov = oldFov; oldFov = null; }
    // Leave the visitor with a wide view of Monaco rather than at street level.
    if (was !== "off") M.view(M.PLACES[1], was === "ended" ? 1.5 : 2.5);
  }

  M.tours.register({ id: "lap", label: "Grand Prix lap", start: begin, stop });
  // Shared with the "Legendary laps" tour (lap-legends.js), which drives a
  // car around the same route and reuses the map, lights and tunnel.
  M.lapKit = {
    P, N, DS, LEN, X, Y, ZG, ZC, TUN, wrap, smooth, smoothstep, tunnelWeight, toWorld, toLocal,
    CORNERS, cornerAt, CORNER_SPOTS, svButton, bounds: { minX, maxX, minY, maxY },
    ui: { map, mapDone, mapCar, mx, my, lights, tunnel },
  };
  // For checks and tuning from the browser console.
  M.lap = { get duration() { return F.DURATION; }, get style() { return styleName; }, length: LEN, seek, get state() { return state; }, get time() { return t; } };
})();
