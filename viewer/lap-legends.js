// "Legendary laps": the three fastest laps of each decade at Monaco, from
// 1950 to today, replayed behind a small generic single-seater. The car
// covers the circuit in exactly the real lap time; how fast it goes in each
// corner and on each straight is estimated from the shape of the road.
//
// Open with ?tour=legends, or ?tour=legends&legend=1985-senna to go straight
// to one lap.
/* global Cesium */
(function () {
  const M = window.MC3D, K = M && M.lapKit;
  if (!M || !M.tours || !K) return;
  const { viewer } = M;
  const scene = viewer.scene, cam = viewer.camera, hud = M.tours.hud;
  const { N, DS, LEN, ZG, wrap, smooth, smoothstep, toWorld, TUN, ui } = K;

  // ---- The laps -----------------------------------------------------------
  // Official pole-position times and race fastest laps, from the race
  // reports on Wikipedia ("<year> Monaco Grand Prix" infoboxes). For each
  // decade: the three quickest drivers, each counted once with their best.
  const LAPS = [
    ["1950s", 1959, "Stirling Moss", "Cooper-Climax", "1:39.6", "pole"],
    ["1950s", 1958, "Tony Brooks", "Vanwall", "1:39.8", "pole"],
    ["1950s", 1959, "Jack Brabham", "Cooper-Climax", "1:40.4", "race"],
    ["1960s", 1967, "Jack Brabham", "Brabham-Repco", "1:27.6", "pole"],
    ["1960s", 1968, "Richard Attwood", "BRM", "1:28.1", "race"],
    ["1960s", 1968, "Graham Hill", "Lotus-Ford", "1:28.2", "pole"],
    ["1970s", 1972, "Emerson Fittipaldi", "Lotus-Ford", "1:21.4", "pole"],
    ["1970s", 1971, "Jackie Stewart", "Tyrrell-Ford", "1:22.2", "race"],
    ["1970s", 1970, "Jochen Rindt", "Lotus-Ford", "1:23.2", "race"],
    ["1980s", 1985, "Ayrton Senna", "Lotus-Renault", "1:20.450", "pole"],
    ["1980s", 1986, "Alain Prost", "McLaren-TAG", "1:22.627", "pole"],
    ["1980s", 1987, "Nigel Mansell", "Williams-Honda", "1:23.039", "pole"],
    ["1990s", 1997, "Heinz-Harald Frentzen", "Williams-Renault", "1:18.216", "pole"],
    ["1990s", 1994, "Michael Schumacher", "Benetton-Ford", "1:18.560", "pole"],
    ["1990s", 1992, "Nigel Mansell", "Williams-Renault", "1:19.495", "pole"],
    ["2000s", 2006, "Fernando Alonso", "Renault", "1:13.962", "pole",
      "Michael Schumacher was quicker, but his time was struck off after he stopped his car at La Rascasse."],
    ["2000s", 2004, "Jarno Trulli", "Renault", "1:13.985", "pole"],
    ["2000s", 2004, "Michael Schumacher", "Ferrari", "1:14.439", "race",
      "The race lap record for the 2003–2014 layout."],
    ["2010s", 2019, "Lewis Hamilton", "Mercedes", "1:10.166", "pole"],
    ["2010s", 2018, "Daniel Ricciardo", "Red Bull-TAG Heuer", "1:10.810", "pole"],
    ["2010s", 2017, "Kimi Räikkönen", "Ferrari", "1:12.178", "pole"],
    ["2020s", 2025, "Lando Norris", "McLaren-Mercedes", "1:09.954", "pole",
      "The fastest pole lap ever set at Monaco."],
    ["2020s", 2024, "Charles Leclerc", "Ferrari", "1:10.270", "pole"],
    ["2020s", 2023, "Max Verstappen", "Red Bull-Honda RBPT", "1:11.365", "pole"],
  ].map(([decade, year, who, team, time, kind, story]) => {
    const [m, sec] = time.split(":");
    const last = who.split(" ").slice(-1)[0];
    return {
      id: `${year}-${last.toLowerCase().normalize("NFD").replace(/[^a-z]/g, "")}`,
      decade, year, who, last, team, time, kind, story,
      secs: Number(m) * 60 + Number(sec), digits: sec.split(".")[1].length,
      layout: layoutOf(year),
    };
  });
  const DECADES = [...new Set(LAPS.map((l) => l.decade))];

  // The circuit was changed over the years. The car always drives today's
  // road; the period length is used for the speed shown on screen.
  function layoutOf(y) {
    if (y <= 1950) return { km: 3.180, note: "The 1950 circuit (3.180 km)." };
    if (y <= 1972) return { km: 3.145, note: "Before 1973 there was no swimming-pool section: the cars ran straight along the quay from Tabac to the Gasometer hairpin (3.145 km)." };
    if (y <= 1975) return { km: 3.278, note: "1973 – 1975 layout with the new swimming-pool section (3.278 km)." };
    if (y <= 1985) return { km: 3.312, note: "1976 – 1985 layout (3.312 km)." };
    if (y <= 1996) return { km: 3.328, note: "1986 – 1996 layout (3.328 km)." };
    if (y <= 2002) return { km: 3.370, note: "1997 – 2002 layout (3.370 km)." };
    if (y <= 2014) return { km: 3.340, note: "2003 – 2014 layout (3.340 km)." };
    return { km: 3.337, note: "Today's layout (3.337 km)." };
  }

  // What the cars could do, roughly, per decade: grip without downforce (g),
  // extra grip from downforce (per metre of corner radius), acceleration and
  // braking (m/s²), top speed at Monaco (km/h) and the car's shape. Only the
  // shape of the speed trace comes from these: overall grip is then tuned
  // until the lap takes exactly the real time.
  const ERAS = {
    "1950s": { mu: 1.0, c: 0, acc: 5, brk: 9, top: 200 },
    "1960s": { mu: 1.1, c: 0, acc: 6, brk: 11, top: 220 },
    "1970s": { mu: 1.3, c: 0.002, acc: 8, brk: 16, top: 250 },
    "1980s": { mu: 1.5, c: 0.004, acc: 10, brk: 28, top: 280 },
    "1990s": { mu: 1.6, c: 0.005, acc: 11, brk: 34, top: 280 },
    "2000s": { mu: 1.7, c: 0.006, acc: 12, brk: 40, top: 290 },
    "2010s": { mu: 1.7, c: 0.0065, acc: 12, brk: 42, top: 290 },
    "2020s": { mu: 1.7, c: 0.0065, acc: 12, brk: 42, top: 290 },
  };
  const shapeOf = (y) => (y < 1968 ? "classic" : y < 2000 ? "winged" : "modern");

  // ---- Route geometry -------------------------------------------------
  const P = K.P;
  const X = K.X, Y = K.Y;
  const CX = smooth(X, 2), CY = smooth(Y, 2);          // the car's line
  const QX = smooth(X, 6), QY = smooth(Y, 6);          // the camera's, calmer
  const CZ = smooth(ZG, 3);
  const HEAD = new Float64Array(N);
  for (let i = 0; i < N; i++) HEAD[i] = Math.atan2(CX[wrap(i + 1)] - CX[wrap(i - 1)], CY[wrap(i + 1)] - CY[wrap(i - 1)]);
  let KAPPA = new Float64Array(N);
  for (let i = 0; i < N; i++) KAPPA[i] = Math.abs(Cesium.Math.negativePiToPi(HEAD[wrap(i + 1)] - HEAD[wrap(i - 1)])) / (2 * DS);
  KAPPA = smooth(KAPPA, 1.5);

  // ---- Speed along the lap, for one lap -------------------------------
  function trace(e, vmax, m) {
    const V = new Float64Array(N);
    for (let i = 0; i < N; i++) {
      const r = 1 / Math.max(1e-5, KAPPA[i]), den = 1 - e.c * m * r;
      V[i] = den <= 0.05 ? vmax : Math.min(vmax, Math.sqrt(e.mu * m * 9.81 * r / den));
    }
    const acc = e.acc * Math.sqrt(m), brk = e.brk * Math.sqrt(m);
    for (let p = 0; p < 2; p++) {        // twice round: the lap is a loop
      for (let i = 0; i < N; i++) {
        const a = wrap(i - 1), push = acc * Math.max(0.15, 1 - V[a] / (vmax * 1.08));
        V[i] = Math.min(V[i], Math.sqrt(V[a] ** 2 + 2 * push * DS));
      }
    }
    for (let p = 0; p < 2; p++) {
      for (let i = N - 1; i >= 0; i--) V[i] = Math.min(V[i], Math.sqrt(V[wrap(i + 1)] ** 2 + 2 * brk * DS));
    }
    const T = new Float64Array(N + 1);
    for (let i = 0; i < N; i++) T[i + 1] = T[i] + DS / ((V[i] + V[wrap(i + 1)]) / 2);
    return { V, T };
  }
  function bake(lap) {
    if (lap.T) return lap;
    const e = ERAS[lap.decade];
    const vmax = (e.top / 3.6) * LEN / (lap.layout.km * 1000);
    let lo = 0.05, hi = 30, r;
    for (let it = 0; it < 48; it++) {
      const m = Math.sqrt(lo * hi);
      r = trace(e, vmax, m);
      if (r.T[N] > lap.secs) lo = m; else hi = m;
    }
    const f = r.T[N] / lap.secs;                     // close the last few ms
    for (let i = 0; i <= N; i++) r.T[i] /= f;
    for (let i = 0; i < N; i++) r.V[i] *= f;
    lap.T = r.T; lap.V = r.V;
    lap.kmh = lap.layout.km * 1000 / LEN * 3.6;      // route m/s → real km/h
    return lap;
  }

  // ---- The car -----------------------------------------------------------
  // Built from simple shapes, x forward, y left, z up, wheels on z = 0.
  // Pearl and gold: no team's colours.
  const COL = {
    body: Cesium.Color.fromCssColorString("#f1eee6"), accent: Cesium.Color.fromCssColorString("#e2b65e"),
    carbon: Cesium.Color.fromCssColorString("#1d2026"), tyre: Cesium.Color.fromCssColorString("#141414"),
    rim: Cesium.Color.fromCssColorString("#a7adb4"), glass: Cesium.Color.fromCssColorString("#3b4652"),
  };
  const VF = Cesium.PerInstanceColorAppearance.VERTEX_FORMAT;
  const inst = (geometry, at, color, rot) => {
    let mm = Cesium.Matrix4.fromTranslation(new Cesium.Cartesian3(...at));
    if (rot) mm = Cesium.Matrix4.multiply(mm, Cesium.Matrix4.fromRotation(rot), new Cesium.Matrix4());
    return new Cesium.GeometryInstance({
      geometry, modelMatrix: mm,
      attributes: { color: Cesium.ColorGeometryInstanceAttribute.fromColor(color) },
    });
  };
  const box = (at, size, color) => inst(Cesium.BoxGeometry.fromDimensions({ dimensions: new Cesium.Cartesian3(...size), vertexFormat: VF }), at, color);
  const blob = (at, radii, color) => inst(new Cesium.EllipsoidGeometry({ radii: new Cesium.Cartesian3(...radii), vertexFormat: VF, stackPartitions: 14, slicePartitions: 18 }), at, color);
  const AXLE = Cesium.Matrix3.fromRotationX(Math.PI / 2);
  const disc = (at, r, w, color) => inst(new Cesium.CylinderGeometry({ length: w, topRadius: r, bottomRadius: r, slices: 22, vertexFormat: VF }), at, color, AXLE);

  const SHAPES = {
    classic: { wb: 2.3, track: 1.25, rF: 0.33, rR: 0.35, wF: 0.17, wR: 0.22, len: 4.0 },
    winged: { wb: 2.7, track: 1.55, rF: 0.32, rR: 0.34, wF: 0.27, wR: 0.42, len: 4.5 },
    modern: { wb: 3.5, track: 1.6, rF: 0.35, rR: 0.36, wF: 0.32, wR: 0.4, len: 5.5 },
  };
  function carParts(shape) {
    const s = SHAPES[shape], h = s.wb / 2, parts = [];
    const wheels = [[h, s.track / 2, s.rF, s.wF], [h, -s.track / 2, s.rF, s.wF], [-h, s.track / 2, s.rR, s.wR], [-h, -s.track / 2, s.rR, s.wR]];
    for (const [x, y, r, w] of wheels) {
      parts.push(disc([x, y, r], r, w, COL.tyre));
      parts.push(disc([x, y + Math.sign(y) * 0.006, r], r * 0.62, w + 0.01, COL.rim));
    }
    if (shape === "classic") {
      parts.push(blob([0.05, 0, 0.47], [h + 0.62, 0.37, 0.32], COL.body));
      parts.push(blob([h + 0.6, 0, 0.45], [0.08, 0.2, 0.17], COL.carbon));          // grille
      parts.push(box([0.1, 0, 0.78], [0.04, 0.34, 0.14], COL.glass));             // screen
      parts.push(blob([-0.22, 0, 0.86], [0.14, 0.13, 0.14], COL.accent));          // helmet
      parts.push(box([-h + 0.1, 0, 0.62], [0.9, 0.08, 0.06], COL.accent));       // stripe
    } else {
      const w = s.track;
      parts.push(box([-0.1, 0, 0.1], [s.wb + 0.5, w * 0.72, 0.05], COL.carbon));     // floor
      parts.push(box([0.15 * s.wb, 0, 0.38], [s.wb * 0.7, 0.55, 0.42], COL.body));   // tub
      parts.push(box([h + 0.35, 0, 0.3], [1.0, 0.3, 0.2], COL.body));               // nose
      parts.push(box([-0.12 * s.wb, 0, 0.33], [s.wb * 0.42, w * 0.82, 0.36], COL.body)); // sidepods
      parts.push(box([-0.33 * s.wb, 0, 0.58], [s.wb * 0.42, 0.42, 0.34], COL.body)); // engine cover
      parts.push(box([-0.1 * s.wb, 0, 0.84], [0.32, 0.28, 0.28], COL.accent));      // airbox
      parts.push(blob([0.04 * s.wb, 0, 0.76], [0.15, 0.13, 0.14], COL.accent));     // helmet
      const fw = w + 0.25;
      parts.push(box([h + 0.7, 0, 0.1], [0.42, fw, 0.04], COL.accent));             // front wing
      parts.push(box([h + 0.7, fw / 2, 0.16], [0.48, 0.03, 0.17], COL.carbon));
      parts.push(box([h + 0.7, -fw / 2, 0.16], [0.48, 0.03, 0.17], COL.carbon));
      const rx = -h - 0.42;
      parts.push(box([rx, 0, 0.94], [0.34, 1.0, 0.05], COL.accent));               // rear wing
      parts.push(box([rx + 0.06, 0, 0.8], [0.26, 1.0, 0.04], COL.carbon));
      parts.push(box([rx, 0.5, 0.78], [0.55, 0.03, 0.46], COL.carbon));
      parts.push(box([rx, -0.5, 0.78], [0.55, 0.03, 0.46], COL.carbon));
      parts.push(box([rx + 0.15, 0, 0.55], [0.12, 0.06, 0.42], COL.carbon));
    }
    return parts;
  }
  const cars = {};
  function makeCar(shape) {
    if (cars[shape]) return cars[shape];
    const s = SHAPES[shape];
    const body = scene.primitives.add(new Cesium.Primitive({
      geometryInstances: carParts(shape),
      appearance: new Cesium.PerInstanceColorAppearance({ flat: false, translucent: false }),
      asynchronous: false, show: false, shadows: Cesium.ShadowMode.DISABLED,
    }));
    // A soft dark patch under the car, so it sits on the road.
    const shadow = scene.primitives.add(new Cesium.Primitive({
      geometryInstances: blob([-0.1, 0, 0.02], [s.len / 2 + 0.2, s.track / 2 + 0.35, 0.012], Cesium.Color.BLACK.withAlpha(0.42)),
      appearance: new Cesium.PerInstanceColorAppearance({ flat: true, translucent: true }),
      asynchronous: false, show: false, shadows: Cesium.ShadowMode.DISABLED,
    }));
    return (cars[shape] = { body, shadow, len: s.len });
  }
  let car = null;
  const showCar = (on) => { for (const c of Object.values(cars)) { c.body.show = on && c === car; c.shadow.show = on && c === car; } };

  // ---- Tunnel: darken everything but what is close, like headlights --------
  const dark = new Cesium.PostProcessStage({
    name: "mc3d_legend_tunnel",
    fragmentShader: `
      uniform sampler2D colorTexture;
      uniform sampler2D depthTexture;
      uniform float amount;
      in vec2 v_textureCoordinates;
      void main() {
        vec4 c = texture(colorTexture, v_textureCoordinates);
        float d = czm_readDepth(depthTexture, v_textureCoordinates);
        float lit = 0.0;
        if (d < 1.0) {
          vec4 eye = czm_windowToEyeCoordinates(gl_FragCoord.xy, d);
          lit = 1.0 - smoothstep(9.0, 34.0, length(eye.xyz));
        }
        vec3 warm = c.rgb * vec3(1.0, 0.86, 0.66) * (0.12 + 0.95 * lit);
        out_FragColor = vec4(mix(c.rgb, warm, amount), 1.0);
      }`,
    uniforms: { amount: () => tunnelDark },
  });
  dark.enabled = false;
  scene.postProcessStages.add(dark);
  let tunnelDark = 0;

  // ---- On-screen: picker, timing card -------------------------------------
  const el = (tag, cls, parent, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    if (parent) parent.appendChild(e);
    return e;
  };
  const picker = el("div", "legends", document.body);
  picker.hidden = true;
  const sheet = el("div", "legends-sheet", picker);
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-label", "Choose a lap");
  const head = el("div", "legends-head", sheet);
  const headText = el("div", "", head);
  el("div", "legends-kicker", headText, "Legendary laps · Monaco");
  el("div", "legends-title", headText, "Ride the fastest laps of each decade");
  const pickX = el("button", "legends-x", head, "✕");
  pickX.setAttribute("aria-label", "Close");
  const decadeRow = el("div", "legends-decades", sheet);
  const list = el("div", "legends-list", sheet);
  const foot = el("div", "legends-foot", sheet);
  let decade = DECADES[DECADES.length - 1];
  const decadeBtns = DECADES.map((d) => {
    const b = el("button", "", decadeRow, d);
    b.onclick = () => { decade = d; fillList(); };
    return b;
  });
  function fillList() {
    decadeBtns.forEach((b) => b.classList.toggle("on", b.textContent === decade));
    const on = decadeBtns.find((b) => b.classList.contains("on"));
    if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest", inline: "center" });
    list.textContent = "";
    const laps = LAPS.filter((l) => l.decade === decade).sort((a, b) => a.secs - b.secs);
    laps.forEach((l, i) => {
      const b = el("button", "legend-card", list);
      if (lap && lap.id === l.id) b.classList.add("current");
      el("span", "legend-rank", b, String(i + 1));
      const who = el("span", "legend-who", b);
      el("b", "", who, l.who);
      el("small", "", who, `${l.year} · ${l.team} · ${l.kind === "pole" ? "Pole lap" : "Race fastest lap"}`);
      el("span", "legend-time", b, l.time);
      b.onclick = () => choose(l);
    });
    const notes = [...new Set(laps.map((l) => l.layout.note))];
    foot.textContent = `${notes.join(" ")} The car follows today's road and takes exactly the real lap time; its speed in each corner is estimated.`;
  }

  const timer = el("div", "legend-timer", hud.root);
  const tWho = el("div", "legend-timer-who", timer);
  const tClock = el("div", "legend-timer-clock", timer);
  const tOf = el("div", "legend-timer-of", timer);
  const tSpeed = el("div", "legend-timer-speed", timer);
  timer.hidden = true;

  // "Preparing the track": before the first lap the camera visits the whole
  // circuit behind this card, so the photoreal tiles are loaded (and kept)
  // before the car sets off, instead of streaming in at 250 km/h.
  const prep = el("div", "legend-prep", document.body);
  prep.hidden = true;
  const prepCard = el("div", "legend-prep-card", prep);
  el("div", "legends-kicker", prepCard, "Preparing the track");
  const prepTitle = el("div", "legend-prep-title", prepCard);
  el("div", "legend-prep-note", prepCard, "Loading the streets along the whole circuit, so the lap runs smoothly.");
  const prepRail = el("div", "legend-prep-rail", prepCard);
  const prepFill = el("div", "legend-prep-fill", prepRail);
  const prepSkip = el("button", "legend-prep-skip", prepCard, "Skip");

  const fmt = (s, digits) => {
    s = Math.max(0, s);
    const m = Math.floor(s / 60), r = s - m * 60;
    const p = 10 ** digits, rr = Math.floor(r * p + 1e-6) / p;
    return `${m}:${rr.toFixed(digits).padStart(digits ? 3 + digits : 2, "0")}`;
  };

  // ---- Where the car and the camera are -------------------------------
  const lerp = (A, u) => { const i = Math.floor(u), f = u - i; return A[wrap(i)] * (1 - f) + A[wrap(i + 1)] * f; };
  function indexAt(T, t) {
    t = Math.min(T[N], Math.max(0, t));
    let lo = 0, hi = N;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (T[mid] <= t) lo = mid; else hi = mid; }
    return lo + (t - T[lo]) / (T[lo + 1] - T[lo]);
  }
  const RUNUP = 230;          // m before the line where the car sets off
  const COAST = 9;            // m/s², easing off after the line
  let lap = null, i0 = 0, tPre = 0, tPost = 0;
  // tl: lap clock, negative on the run-up, past the lap time while slowing down.
  function uAt(tl) {
    if (tl < 0) return indexAt(lap.T, lap.T[N] + tl) - N;
    if (tl <= lap.secs) return indexAt(lap.T, tl);
    const v0 = lap.V[0], tau = Math.min(tl - lap.secs, v0 / COAST);
    return (v0 * tau - COAST * tau * tau / 2) / DS;
  }
  function speedAt(tl, u) {
    if (tl > lap.secs) return Math.max(0, lap.V[0] - COAST * (tl - lap.secs));
    return lerp(lap.V, u);
  }

  let zOff = 0, zOffOk = false, frame = 0;
  const local = new Cesium.Cartesian3(), world = new Cesium.Cartesian3();
  const rot = new Cesium.Matrix3(), localM = new Cesium.Matrix4(), carM = new Cesium.Matrix4();
  function placeCar(u) {
    const clean = M.cleanTrack && M.cleanTrack.on;
    if (clean) zOff = M.cleanTrack.lift;          // on the modelled track
    const x = lerp(CX, u), y = lerp(CY, u), z = lerp(CZ, u) + zOff;
    const a = u + 1.2 / DS, b = u - 1.2 / DS;
    let fx = lerp(CX, a) - lerp(CX, b), fy = lerp(CY, a) - lerp(CY, b), fz = lerp(CZ, a) - lerp(CZ, b);
    const l = Math.hypot(fx, fy, fz) || 1;
    fx /= l; fy /= l; fz /= l;
    let lx = -fy, ly = fx;                                   // left = up × forward
    const ll = Math.hypot(lx, ly) || 1;
    lx /= ll; ly /= ll;
    const ux = -fz * ly, uy = fz * lx, uz = fx * ly - fy * lx; // up = forward × left
    Cesium.Matrix3.fromArray([fx, fy, fz, lx, ly, 0, ux, uy, uz], 0, rot);
    Cesium.Matrix4.fromRotationTranslation(rot, Cesium.Cartesian3.fromElements(x, y, z, local), localM);
    Cesium.Matrix4.multiply(toWorld, localM, carM);
    car.body.modelMatrix = carM;
    car.shadow.modelMatrix = carM;
    return { x, y, z };
  }
  // Keep the car on the ground of whichever 3D view is shown: the route
  // heights come from the LiDAR and can sit a little above or below it.
  function settle(pos) {
    if (++frame % 3 || !scene.clampToHeightSupported || (M.cleanTrack && M.cleanTrack.on)) return;
    Cesium.Matrix4.multiplyByPoint(toWorld, Cesium.Cartesian3.fromElements(pos.x, pos.y, pos.z - zOff, local), world);
    let hit;
    try { hit = scene.clampToHeight(world, [car.body, car.shadow]); } catch (e) { hit = undefined; }
    if (!hit) return;
    Cesium.Matrix4.multiplyByPoint(K.toLocal, hit, local);
    const d = local.z - (pos.z - zOff);
    if (Math.abs(d) > 2.5) return;                          // a roof or a bridge
    zOff = zOffOk ? zOff + (d - zOff) * 0.2 : d;
    zOffOk = true;
  }

  let view = "chase";
  const zUp = new Cesium.Cartesian3(0, 0, 1);
  const dir = new Cesium.Cartesian3(), up = new Cesium.Cartesian3(), right = new Cesium.Cartesian3(), eye = new Cesium.Cartesian3();
  function cameraFor(u, v) {
    let ex, ey, ez, tx, ty, tz;
    if (view === "chase") {
      const back = 8.5 + 0.035 * v, uc = u - back / DS, ut = u + 2.5 / DS;
      ex = lerp(QX, uc); ey = lerp(QY, uc); ez = Math.max(lerp(CZ, uc), lerp(CZ, u)) + zOff + 2.6;
      tx = lerp(CX, ut); ty = lerp(CY, ut); tz = lerp(CZ, ut) + zOff + 0.75;
    } else {
      const ua = u + 0.4 / DS, ut = u + 30 / DS;
      ex = lerp(CX, ua); ey = lerp(CY, ua); ez = lerp(CZ, ua) + zOff + 1.05;
      tx = lerp(QX, ut); ty = lerp(QY, ut); tz = lerp(CZ, ut) + zOff + 0.9;
    }
    Cesium.Cartesian3.fromElements(tx - ex, ty - ey, tz - ez, dir);
    Cesium.Cartesian3.normalize(dir, dir);
    Cesium.Cartesian3.cross(dir, zUp, right);
    Cesium.Cartesian3.normalize(right, right);
    Cesium.Cartesian3.cross(right, dir, up);
    Cesium.Matrix4.multiplyByPoint(toWorld, Cesium.Cartesian3.fromElements(ex, ey, ez, eye), eye);
    Cesium.Matrix4.multiplyByPointAsVector(toWorld, dir, dir);
    Cesium.Matrix4.multiplyByPointAsVector(toWorld, up, up);
    return { destination: eye, orientation: { direction: dir, up } };
  }

  // ---- Playing ---------------------------------------------------------
  const ctrl = scene.screenSpaceCameraController;
  let state = "off";          // off | pick | intro | playing | paused | ended
  let tl = 0, last = 0, run = 0, looked = false, flying = false, timers = [];
  let oldMinZoom = null, oldFov = null;
  const later = (fn, ms) => { const id = setTimeout(fn, ms); timers.push(id); return id; };
  const clearTimers = () => { timers.forEach(clearTimeout); timers = []; };

  function draw() {
    const u = uAt(tl), v = speedAt(tl, u);
    const pos = placeCar(u);
    settle(pos);
    const s = ((u % N) + N) % N * DS;
    if (tl < 0) hud.caption(`${lap.year} · ${lap.kind === "pole" ? "Pole lap" : "Race fastest lap"}`, lap.who, `${lap.team} · flying lap, timing starts at the line`);
    else if (tl > lap.secs) hud.caption("Lap complete", lap.time, lap.who);
    else { const c = K.cornerAt(s); hud.caption(c.k, c.t, c.n); }
    const lapF = tl <= 0 ? 0 : tl >= lap.secs ? 1 : s / LEN;
    hud.progress(lapF);
    ui.mapDone.setAttribute("stroke-dasharray", `${lapF.toFixed(4)} 1`);
    const i = wrap(Math.round(u));
    ui.mapCar.setAttribute("cx", ui.mx(i)); ui.mapCar.setAttribute("cy", ui.my(i));
    tClock.textContent = fmt(Math.min(Math.max(tl, 0), lap.secs), lap.digits);
    timer.classList.toggle("done", tl >= lap.secs);
    tSpeed.textContent = `${Math.round(v * lap.kmh)} km/h`;
    // Tunnel.
    let d = 0;
    for (const [a, b] of TUN) d = Math.max(d, smoothstep(a - 30, a - 4, s) * (1 - smoothstep(b + 4, b + 40, s)));
    if (M.cleanTrack && M.cleanTrack.on) d = 0;     // the modelled tunnel has its own lights
    tunnelDark = d;
    dark.enabled = d > 0.002;
    ui.tunnel.style.opacity = (d * 0.85).toFixed(3);
    return { u, v };
  }
  function applyCamera() {
    const u = uAt(tl);
    cam.setView(cameraFor(u, speedAt(tl, u)));
  }
  function tick() {
    if (state === "off" || state === "pick" && !lap) return;
    if (state === "paused" && !looked && !flying) applyCamera();
    if ((state === "playing" || state === "intro" || flying) && ctrl.enableInputs) ctrl.enableInputs = false;
    if (state !== "playing") return;
    const now = performance.now();
    tl += Math.min(0.5, (now - last) / 1000);     // real time, so the lap takes the real lap time
    last = now;
    if (tl >= tPost) { tl = tPost; draw(); applyCamera(); finish(); return; }
    draw();
    applyCamera();
  }
  scene.preUpdate.addEventListener(tick);

  function setFov() {
    const f = cam.frustum;
    if (!(f instanceof Cesium.PerspectiveFrustum)) return;
    if (oldFov === null) oldFov = f.fov;
    const c = scene.canvas, portrait = c.clientHeight > c.clientWidth;
    const deg = view === "chase" ? (portrait ? 80 : 62) : (portrait ? 88 : 72);
    f.fov = Cesium.Math.toRadians(deg);
  }
  // The timing card sits just under the mini map.
  function placeTimer() {
    const r = ui.map.getBoundingClientRect();
    if (r.height) timer.style.top = `${Math.round(r.bottom + 8)}px`;
  }
  window.addEventListener("resize", () => { if (state !== "off") { setFov(); placeTimer(); } });

  function play() {
    clearTimers();
    ui.lights.classList.remove("show");
    const go = () => {
      state = "playing"; looked = false; last = performance.now();
      ctrl.enableInputs = false; hud.playing(true); hud.tip("");
    };
    if (looked) {             // the visitor looked around: glide back first
      const p = cameraFor(uAt(tl), speedAt(tl, uAt(tl))), mine = ++run;
      ctrl.enableInputs = false; flying = true; hud.playing(true); hud.tip("");
      cam.flyTo({
        destination: Cesium.Cartesian3.clone(p.destination),
        orientation: { direction: Cesium.Cartesian3.clone(p.orientation.direction), up: Cesium.Cartesian3.clone(p.orientation.up) },
        duration: 0.8,
        complete: () => { flying = false; if (mine === run && state === "paused") go(); },
        cancel: () => { flying = false; },
      });
    } else go();
  }
  function pause(byTouch) {
    if (state !== "playing") return;
    state = "paused";
    ctrl.enableInputs = true;
    hud.playing(false);
    hud.tip(byTouch ? "Paused · drag to look around · ▶ to continue" : "Paused · drag to look around");
  }
  function toggle() {
    if (state === "intro") { cam.cancelFlight(); run++; clearTimers(); ui.lights.classList.remove("show"); draw(); applyCamera(); play(); }
    else if (state === "playing") pause(false);
    else if (state === "paused") play();
    else if (state === "ended") begin(lap);
    else if (state === "pick" && lap) { hidePicker(); play(); }
  }
  function seek(f) {
    if (!lap || state === "off") return;
    if (state === "intro" || state === "ended" || state === "pick") {
      cam.cancelFlight(); run++; clearTimers(); hidePicker(); ui.lights.classList.remove("show");
      hud.show(handlers); state = "paused"; ctrl.enableInputs = true;
    }
    const target = Math.min(N - 1, Math.max(0, f) * N);
    tl = Math.min(lap.secs - 0.05, lap.T[Math.floor(target)] + (target % 1) * (lap.T[Math.floor(target) + 1] - lap.T[Math.floor(target)]));
    looked = false;
    draw();
    applyCamera();
  }
  scene.canvas.addEventListener("pointerdown", () => {
    if (state === "playing") pause(true);
    if (state === "paused") looked = true;
  });

  function switchView() {
    view = view === "chase" ? "onboard" : "chase";
    hud.buttons[0].innerHTML = view === "chase" ? ICON_ONBOARD : ICON_CHASE;
    hud.buttons[0].title = view === "chase" ? "Onboard camera" : "Chase camera";
    showCar(view === "chase" && state !== "off");
    setFov();
    if (state !== "playing") { looked = false; applyCamera(); }
  }
  const ICON_ONBOARD = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="10" cy="10" r="7.2"/><circle cx="10" cy="10" r="1.6" fill="currentColor"/><path d="M3 10h5.4M11.6 10H17M10 11.6V17"/></svg>';
  const ICON_CHASE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor"><path d="M3 12.5h14v2.2H3zM6 9.5h8l1.2 3H4.8zM8.6 7.4h2.8v2.2H8.6z"/><circle cx="5.2" cy="15" r="1.7"/><circle cx="14.8" cy="15" r="1.7"/></svg>';
  const ICON_LIST = '<svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor"><rect x="3" y="4" width="14" height="2.2" rx="1"/><rect x="3" y="9" width="14" height="2.2" rx="1"/><rect x="3" y="14" width="14" height="2.2" rx="1"/></svg>';

  const handlers = {
    onToggle: toggle, onSeek: seek, onReplay: () => begin(lap),
    marks: K.CORNERS.slice(1).map((c) => c.s / LEN),
    buttons: [
      { html: ICON_ONBOARD, title: "Onboard camera", onClick: () => switchView() },
      { html: ICON_LIST, title: "Choose another lap", onClick: () => openPicker() },
    ],
    more: { label: "Other laps", onClick: () => openPicker() },
  };

  function openPicker() {
    if (state === "playing") pause(false);
    if (state === "intro") { cam.cancelFlight(); run++; clearTimers(); ui.lights.classList.remove("show"); state = lap ? "paused" : "pick"; }
    if (!lap) state = "pick";
    if (lap) decade = lap.decade;
    picker.hidden = false;
    document.body.classList.add("picking");
    fillList();
  }
  function hidePicker() { picker.hidden = true; document.body.classList.remove("picking"); }
  pickX.onclick = () => { hidePicker(); if (!lap) M.tours.stop(); };
  picker.addEventListener("pointerdown", (e) => { if (e.target === picker) pickX.onclick(); });

  function choose(l) { hidePicker(); begin(l); }

  function start() {
    state = "pick";
    if (M.setTourQuality) M.setTourQuality(true);
    if (M.cleanTrack) M.cleanTrack.show(true);
    lap = null;
    const photoreal = document.body.classList.contains("photoreal");
    if (M.getMode() !== "surface" && !photoreal) M.setMode("surface");
    if (oldMinZoom === null) { oldMinZoom = ctrl.minimumZoomDistance; ctrl.minimumZoomDistance = 1; }
    hud.show(handlers);
    hud.caption("Legendary laps", "Circuit de Monaco", "Pick a driver to ride along at their real lap time");
    hud.progress(0);
    timer.hidden = true;
    for (const e of [ui.map, ui.tunnel]) e.hidden = false;
    ui.tunnel.classList.add("lamps");
    ui.tunnel.style.opacity = 0;
    // A first look at the start line while choosing.
    const p = cameraFor(N - 60 / DS, 0);
    cam.flyTo({ destination: Cesium.Cartesian3.clone(p.destination), orientation: { direction: Cesium.Cartesian3.clone(p.orientation.direction), up: Cesium.Cartesian3.clone(p.orientation.up) }, duration: 3 });
    const want = new URLSearchParams(location.search).get("legend");
    const l = want && LAPS.find((x) => x.id === want.toLowerCase());
    if (l) begin(l); else openPicker();
  }

  function begin(l) {
    clearTimers();
    hidePicker();
    const mine = ++run;
    lap = bake(l);
    i0 = N - Math.round(RUNUP / DS);
    tPre = lap.T[N] - lap.T[i0];
    tPost = lap.secs + lap.V[0] / COAST + 0.3;
    tl = -tPre;
    state = "intro";
    looked = false;
    ctrl.enableInputs = false;
    zOffOk = false; zOff = 0;
    car = makeCar(shapeOf(lap.year));
    showCar(view === "chase");
    setFov();
    hud.show(handlers);
    hud.buttons[0].innerHTML = view === "chase" ? ICON_ONBOARD : ICON_CHASE;
    hud.buttons[0].title = view === "chase" ? "Onboard camera" : "Chase camera";
    tWho.textContent = `${lap.last.toUpperCase()} · ${lap.year}`;
    tOf.textContent = `/ ${lap.time}`;
    timer.hidden = false;
    for (const e of [ui.map, ui.tunnel, ui.lights]) e.hidden = false;
    placeTimer();
    ui.lights.querySelectorAll("i").forEach((d) => d.classList.remove("on"));
    draw();
    hud.caption(`${lap.year} · ${lap.kind === "pole" ? "Pole lap" : "Race fastest lap"}`, lap.who,
      lap.story || `${lap.team} · ${lap.time} on the ${lap.layout.km.toFixed(3)} km circuit`);
    const google = M.getGoogle && M.getGoogle();
    const intro = (fly) => {
      if (mine !== run || state !== "intro") return;
      const p = cameraFor(uAt(tl), 0);
      if (!fly) { cam.setView(p); later(() => startLights(mine), 400); return; }
      cam.flyTo({
        destination: Cesium.Cartesian3.clone(p.destination),
        orientation: { direction: Cesium.Cartesian3.clone(p.orientation.direction), up: Cesium.Cartesian3.clone(p.orientation.up) },
        duration: 2.5, easingFunction: Cesium.EasingFunction.CUBIC_IN_OUT,
        complete: () => { if (mine === run && state === "intro") startLights(mine); },
      });
    };
    if (google && !prepared) prepare(google, mine).then((done) => { if (done) intro(false); });
    else intro(true);
  }

  let prepared = false;
  const PREP_STEP = 90;          // m between the places the camera visits
  const frameDone = () => new Promise((res) => {
    const off = scene.postRender.addEventListener(() => { off(); res(); });
  });
  async function prepare(google, mine) {
    cam.cancelFlight();
    prepTitle.textContent = `${lap.who} · ${lap.year}`;
    prepFill.style.width = "0%";
    prep.hidden = false;
    let skip = false;
    prepSkip.onclick = () => { skip = true; };
    // From just after the start round to the run-up, so the first metres
    // of the lap are the freshest in the cache.
    const stops = [];
    for (let s = PREP_STEP; s < LEN; s += PREP_STEP) stops.push(s / DS);
    stops.push(-RUNUP / DS, -RUNUP / 2 / DS, 0, 30 / DS);
    for (let k = 0; k < stops.length && !skip; k++) {
      if (mine !== run) break;
      const u = stops[k];
      cam.setView(cameraFor(u, lap.V[wrap(Math.round(u))]));
      const t0 = performance.now();
      let frames = 0;
      while (!skip && mine === run) {
        await frameDone();
        frames++;
        if ((frames > 3 && google.tilesLoaded) || performance.now() - t0 > 2000) break;
      }
      prepFill.style.width = `${((k + 1) / stops.length * 100).toFixed(1)}%`;
    }
    prep.hidden = true;
    if (mine !== run) return false;
    prepared = !skip;
    return true;
  }
  function startLights(mine) {
    const dots = [...ui.lights.querySelectorAll("i")];
    ui.lights.classList.add("show");
    const wait = lap.story ? 1800 : 600;           // time to read the story
    dots.forEach((d, i) => later(() => d.classList.add("on"), wait + i * 380));
    later(() => {
      if (mine !== run || state !== "intro") return;
      dots.forEach((d) => d.classList.remove("on"));
      play();
    }, wait + 5 * 380 + 600);
  }
  function finish() {
    state = "ended";
    run++;
    ctrl.enableInputs = true;
    tunnelDark = 0; dark.enabled = false;
    hud.progress(1);
    const avg = lap.layout.km / lap.secs * 3600;
    later(() => {
      if (state === "ended") hud.finish(`${lap.year} · ${lap.kind === "pole" ? "Pole lap" : "Race fastest lap"}`, `${lap.who} · ${lap.time}`, `${avg.toFixed(1)} km/h on average over ${lap.layout.km.toFixed(3)} km, ${lap.team}`);
    }, 600);
  }
  function stop() {
    const was = state;
    prep.hidden = true;
    if (M.setTourQuality) M.setTourQuality(false);
    if (M.cleanTrack) M.cleanTrack.show(false);
    state = "off";
    run++;
    clearTimers();
    cam.cancelFlight();
    hidePicker();
    ctrl.enableInputs = true;
    flying = false;
    showCar(false);
    tunnelDark = 0; dark.enabled = false;
    timer.hidden = true;
    ui.tunnel.classList.remove("lamps");
    for (const e of [ui.map, ui.tunnel, ui.lights]) e.hidden = true;
    ui.lights.classList.remove("show");
    if (oldMinZoom !== null) { ctrl.minimumZoomDistance = oldMinZoom; oldMinZoom = null; }
    if (oldFov !== null && cam.frustum instanceof Cesium.PerspectiveFrustum) { cam.frustum.fov = oldFov; oldFov = null; }
    if (was !== "off") M.view(M.PLACES[1], 2);
  }

  M.tours.register({ id: "legends", label: "Legendary laps", start, stop });
  // For checks from the browser console.
  M.legends = {
    LAPS, choose, seek, bake,
    get state() { return state; }, get clock() { return tl; }, get lap() { return lap; },
    set view(v) { if (v !== view) switchView(); },
  };
})();
