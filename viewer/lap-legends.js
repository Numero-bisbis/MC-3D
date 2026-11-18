// "Legendary laps": the three fastest laps of each decade at Monaco, from
// 1950 to today, replayed behind a car of the period in its team colours. The car
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

  // ---- Route geometry -------------------------------------------------
  const P = K.P;
  const X = K.X, Y = K.Y;
  // The middle of the modelled track (clean-track.js draws it 10 m wide
  // around this same line).
  const MX = smooth(X, 2), MY = smooth(Y, 2);
  const CZ = smooth(ZG, 3);

  // ---- Racing line ----------------------------------------------------
  // Drivers don't follow the middle of the road: they use its whole width
  // to make each corner as open as possible, from the outside on the way
  // in, to the kerb at the apex, and back out to the edge on the way out.
  // That is the "minimum curvature" line: the path inside the track whose
  // bends are as gentle as possible overall. It is found by moving each
  // point sideways (n, metres, + = left) within the track and relaxing the
  // whole loop until the sum of squared bends is smallest.
  const HALF_TRACK = 5.0;                 // clean-track.js HALF
  const EDGE = HALF_TRACK - 1.2;          // car centre to edge: wheels just on the kerbs
  const LX = new Float64Array(N), LY = new Float64Array(N);   // left normal of the middle line
  for (let i = 0; i < N; i++) {
    const dx = MX[wrap(i + 1)] - MX[wrap(i - 1)], dy = MY[wrap(i + 1)] - MY[wrap(i - 1)], l = Math.hypot(dx, dy) || 1;
    LX[i] = -dy / l; LY[i] = dx / l;
  }
  function racingOffsets() {
    // Solved on every 4th point (about 8 m apart), which is plenty for a
    // racing line and converges quickly, then brought back to every point.
    const STEP = 4, M = Math.floor(N / STEP);
    const at = (k) => ((k % M) + M) % M;
    const cx = new Float64Array(M), cy = new Float64Array(M), lx = new Float64Array(M), ly = new Float64Array(M);
    for (let k = 0; k < M; k++) { const i = k * STEP; cx[k] = MX[i]; cy[k] = MY[i]; lx[k] = LX[i]; ly[k] = LY[i]; }
    const n = new Float64Array(M), px = cx.slice(), py = cy.slice();
    for (let sweep = 0; sweep < 4000; sweep++) {
      let moved = 0;
      for (let k = 0; k < M; k++) {
        // Bends at k-1, k and k+1 involve point k with weights 1, -2, 1.
        // The best sideways shift of point k for the sum of their squares
        // is a one-line least-squares step; then keep it on the track.
        const a2 = at(k - 2), a1 = at(k - 1), b1 = at(k + 1), b2 = at(k + 2);
        // Sum of (weight x bend) along the normal, without point k's own share.
        const gx = (px[a2] - 2 * px[a1]) - 2 * (px[a1] + px[b1]) + (px[b2] - 2 * px[b1]);
        const gy = (py[a2] - 2 * py[a1]) - 2 * (py[a1] + py[b1]) + (py[b2] - 2 * py[b1]);
        // Point k contributes (1 + 4 + 1) x its position; solve for n.
        const best = -((gx + 6 * cx[k]) * lx[k] + (gy + 6 * cy[k]) * ly[k]) / 6;
        const nk = Math.max(-EDGE, Math.min(EDGE, n[k] + 1.6 * (best - n[k])));   // over-relaxed
        moved = Math.max(moved, Math.abs(nk - n[k]));
        n[k] = nk; px[k] = cx[k] + nk * lx[k]; py[k] = cy[k] + nk * ly[k];
      }
      if (moved < 1e-4) break;
    }
    const out = new Float64Array(N);
    for (let i = 0; i < N; i++) {
      const k = Math.floor(i / STEP), f = i / STEP - k;
      out[i] = n[at(k)] * (1 - f) + n[at(k + 1)] * f;
    }
    const sm = smooth(out, 2);
    for (let i = 0; i < N; i++) sm[i] = Math.max(-EDGE, Math.min(EDGE, sm[i]));
    return sm;
  }
  const RACE_OFF = new URLSearchParams(location.search).get("line") === "middle" ? new Float64Array(N) : racingOffsets();
  const CX = new Float64Array(N), CY = new Float64Array(N);          // the car's line
  for (let i = 0; i < N; i++) { CX[i] = MX[i] + RACE_OFF[i] * LX[i]; CY[i] = MY[i] + RACE_OFF[i] * LY[i]; }
  const QX = smooth(CX, 6), QY = smooth(CY, 6);          // the camera's, calmer
  const HEAD = new Float64Array(N), SEG = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    HEAD[i] = Math.atan2(CX[wrap(i + 1)] - CX[wrap(i - 1)], CY[wrap(i + 1)] - CY[wrap(i - 1)]);
    SEG[i] = Math.hypot(CX[wrap(i + 1)] - CX[i], CY[wrap(i + 1)] - CY[i]);
  }
  let KAPPA = new Float64Array(N);
  for (let i = 0; i < N; i++) KAPPA[i] = Math.abs(Cesium.Math.negativePiToPi(HEAD[wrap(i + 1)] - HEAD[wrap(i - 1)])) / (SEG[wrap(i - 1)] + SEG[i]);
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
        V[i] = Math.min(V[i], Math.sqrt(V[a] ** 2 + 2 * push * SEG[a]));
      }
    }
    for (let p = 0; p < 2; p++) {
      for (let i = N - 1; i >= 0; i--) V[i] = Math.min(V[i], Math.sqrt(V[wrap(i + 1)] ** 2 + 2 * brk * SEG[i]));
    }
    const T = new Float64Array(N + 1);
    for (let i = 0; i < N; i++) T[i + 1] = T[i] + SEG[i] / ((V[i] + V[wrap(i + 1)]) / 2);
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
  // Built from simple shapes, x forward, y left, z up, wheels on z = 0, in
  // the colours each team ran that year. Colour schemes only: no sponsor
  // names, logos or other marks.
  const hex = (c) => Cesium.Color.fromCssColorString(c);
  const COL = {
    carbon: hex("#1b1d22"), tyre: hex("#131313"), rim: hex("#a7adb4"), glass: hex("#2f3a45"),
    metal: hex("#8d9399"), wire: hex("#c9cdd2"),
  };
  // body: main colour · accent: stripes, airbox, fins · wing: wing planes
  // nose: nose tip or nose band · lower: lower sidepods · helmet.
  // Pass a list for a stripe pair.
  const LIVERY = {
    "1959-moss": { body: "#16285a", accent: "#f2f2ee", nose: "#f2f2ee", helmet: "#f2f2ee", band: true },      // Rob Walker blue, white nose band
    "1958-brooks": { body: "#0f3b26", accent: "#e8e2c8", helmet: "#e6e6e0" },                                 // Vanwall green
    "1959-brabham": { body: "#124a2c", accent: "#f2f2ee", helmet: "#f2f2ee", stripes: true },                 // Cooper works green, white stripes
    "1967-brabham": { body: "#1a4d2e", accent: "#e8b923", helmet: "#f2f2ee", stripes: true },                 // Brabham green and gold
    "1968-attwood": { body: "#12402a", accent: "#e46c1a", nose: "#e46c1a", helmet: "#f2f2ee", band: true },   // BRM green, orange band
    "1968-hill": { body: "#c41a1e", accent: "#f4f1e8", wing: "#c41a1e", helmet: "#0d1b3d", stripes: true, gold: "#c9a64a" }, // red, white and gold
    "1970-rindt": { body: "#c41a1e", accent: "#f4f1e8", wing: "#c41a1e", helmet: "#f4f1e8", stripes: true, gold: "#c9a64a" },
    "1972-fittipaldi": { body: "#0c0c0e", accent: "#c9a64a", wing: "#0c0c0e", helmet: "#f2f2ee", pin: true }, // black and gold
    "1971-stewart": { body: "#1f3d8f", accent: "#f2f2ee", wing: "#1f3d8f", helmet: "#f2f2ee", stripes: true },  // French blue
    "1985-senna": { body: "#0c0c0e", accent: "#c9a64a", wing: "#0c0c0e", helmet: "#f2d21b", pin: true },      // black and gold
    "1986-prost": { body: "#f4f2ee", accent: "#e8332a", top: "#e8332a", wing: "#e8332a", helmet: "#f4f2ee" },  // white, fluorescent red top
    "1987-mansell": { body: "#f4f2ee", accent: "#f2c400", lower: "#1b2f6e", wing: "#1b2f6e", helmet: "#d22a2a" }, // white, navy, yellow
    "1997-frentzen": { body: "#f4f2ee", accent: "#c8a24a", lower: "#14285c", wing: "#14285c", helmet: "#f4f2ee" },  // white and dark blue
    "1994-schumacher": { body: "#3c8fd0", accent: "#2fa84f", top: "#f2f2ee", wing: "#3c8fd0", helmet: "#f2f2ee", nose: "#f2d21b" }, // light blue, green, yellow
    "1992-mansell": { body: "#f4f2ee", accent: "#f2c400", lower: "#1b3a8a", wing: "#1b3a8a", helmet: "#d22a2a" },   // white, blue, yellow
    "2006-alonso": { body: "#2e6fd1", accent: "#f2c400", nose: "#f2c400", wing: "#f2c400", helmet: "#2a5ac0" },      // light blue and yellow
    "2004-trulli": { body: "#2e6fd1", accent: "#f2c400", nose: "#f2c400", wing: "#2e6fd1", helmet: "#f2f2ee" },
    "2004-schumacher": { body: "#d10f16", accent: "#f2f2ee", wing: "#d10f16", helmet: "#d10f16" },                   // Ferrari red
    "2019-hamilton": { body: "#b8bec4", accent: "#00a19b", top: "#c9ced3", wing: "#1b1d22", helmet: "#f2d21b" },    // silver and teal
    "2018-ricciardo": { body: "#1d2750", accent: "#d6252f", nose: "#f2c400", wing: "#1d2750", helmet: "#f2f2ee" },  // matte navy, red, yellow
    "2017-raikkonen": { body: "#d10f16", accent: "#f2f2ee", wing: "#d10f16", helmet: "#1b1d22" },
    "2025-norris": { body: "#ff8000", accent: "#1b1d22", lower: "#2a2d33", wing: "#ff8000", helmet: "#e6f21b" },    // papaya and anthracite
    "2024-leclerc": { body: "#d10f16", accent: "#f2d21b", wing: "#d10f16", helmet: "#d10f16" },
    "2023-verstappen": { body: "#1d2750", accent: "#d6252f", nose: "#f2c400", wing: "#1d2750", helmet: "#f07a1a" },
  };
  const liveryOf = (lap) => {
    const l = LIVERY[lap.id] || { body: "#f1eee6", accent: "#e2b65e", helmet: "#e2b65e" };
    const c = (v, d) => (v ? hex(v) : d);
    const body = hex(l.body), accent = hex(l.accent);
    return {
      body, accent, wing: c(l.wing, COL.carbon), nose: c(l.nose, body), lower: c(l.lower, body), top: c(l.top, body),
      helmet: hex(l.helmet), gold: c(l.gold, accent), band: !!l.band, stripes: !!l.stripes, pin: !!l.pin,
    };
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
  const box = (at, size, color, rot) => inst(Cesium.BoxGeometry.fromDimensions({ dimensions: new Cesium.Cartesian3(...size), vertexFormat: VF }), at, color, rot);
  const blob = (at, radii, color, rot) => inst(new Cesium.EllipsoidGeometry({ radii: new Cesium.Cartesian3(...radii), vertexFormat: VF, stackPartitions: 16, slicePartitions: 22 }), at, color, rot);
  const AXLE = Cesium.Matrix3.fromRotationX(Math.PI / 2);
  const disc = (at, r, w, color) => inst(new Cesium.CylinderGeometry({ length: w, topRadius: r, bottomRadius: r, slices: 28, vertexFormat: VF }), at, color, AXLE);
  const pitch = (deg) => Cesium.Matrix3.fromRotationY(Cesium.Math.toRadians(deg));
  const Z = new Cesium.Cartesian3(0, 0, 1);
  // A round bar from a to b (suspension arms, halo, roll hoop, exhausts).
  function rod(a, b, r, color, r2) {
    const d = new Cesium.Cartesian3(b[0] - a[0], b[1] - a[1], b[2] - a[2]), len = Cesium.Cartesian3.magnitude(d);
    Cesium.Cartesian3.normalize(d, d);
    const axis = Cesium.Cartesian3.cross(Z, d, new Cesium.Cartesian3()), s = Cesium.Cartesian3.magnitude(axis);
    const rot = s < 1e-6 ? (d.z > 0 ? Cesium.Matrix3.IDENTITY : Cesium.Matrix3.fromRotationX(Math.PI))
      : Cesium.Matrix3.fromQuaternion(Cesium.Quaternion.fromAxisAngle(Cesium.Cartesian3.normalize(axis, axis), Math.acos(Cesium.Math.clamp(d.z, -1, 1))));
    return inst(new Cesium.CylinderGeometry({ length: len, topRadius: r2 ?? r, bottomRadius: r, slices: 12, vertexFormat: VF }),
      [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2], color, rot);
  }
  // A cone along x: from radius r0 at x0 to radius r1 at x1, dropping by dz.
  function cone(x0, x1, z0, z1, r0, r1, color, sy = 1, sz = 1) {
    const g = new Cesium.CylinderGeometry({ length: Math.hypot(x1 - x0, z1 - z0), topRadius: r1, bottomRadius: r0, slices: 24, vertexFormat: VF });
    const tilt = Math.atan2(z0 - z1, x1 - x0);
    const rot = Cesium.Matrix3.multiply(Cesium.Matrix3.fromRotationY(Math.PI / 2 + tilt), Cesium.Matrix3.fromScale(new Cesium.Cartesian3(sz, sy, 1)), new Cesium.Matrix3());
    return inst(g, [(x0 + x1) / 2, 0, (z0 + z1) / 2], color, rot);
  }

  // Era of each car: proportions in metres (wheelbase, track, tyre radius
  // and width front/rear, overall length) and which parts it has.
  function eraOf(y) {
    if (y < 1968) return { kind: "cigar", wb: 2.3, track: 1.25, rF: 0.29, rR: 0.31, wF: 0.15, wR: 0.2, len: 3.7 };
    if (y < 1978) return { kind: "wedge", wb: 2.5, track: 1.45, rF: 0.29, rR: 0.32, wF: 0.24, wR: 0.38, len: 4.2, rearWing: y >= 1969, airbox: y >= 1971 };
    if (y < 1989) return { kind: "turbo", wb: 2.8, track: 1.72, rF: 0.3, rR: 0.32, wF: 0.3, wR: 0.42, len: 4.4, noseZ: 0.3 };
    if (y < 2000) return { kind: "modern", wb: 2.9, track: 1.66, rF: 0.32, rR: 0.33, wF: 0.3, wR: 0.38, len: 4.5, highNose: y >= 1993, airbox: true, wideWing: false };
    if (y < 2009) return { kind: "modern", wb: 3.1, track: 1.45, rF: 0.33, rR: 0.33, wF: 0.3, wR: 0.36, len: 4.6, highNose: true, airbox: true, wideWing: false, aero: true };
    if (y < 2022) return { kind: "modern", wb: y >= 2017 ? 3.6 : 3.4, track: y >= 2017 ? 1.62 : 1.48, rF: 0.33, rR: 0.33, wF: y >= 2017 ? 0.305 : 0.25, wR: y >= 2017 ? 0.405 : 0.33, len: 5.4, highNose: false, airbox: true, wideWing: true, aero: true, halo: y >= 2018, fin: y >= 2017 && y <= 2018 };
    return { kind: "modern", wb: 3.6, track: 1.62, rF: 0.36, rR: 0.36, wF: 0.3, wR: 0.405, len: 5.6, highNose: false, airbox: true, wideWing: true, halo: true, ground: true, lowProfile: true };
  }

  function wheels(e, parts) {
    const h = e.wb / 2;
    for (const [x, y, r, w] of [[h, e.track / 2, e.rF, e.wF], [h, -e.track / 2, e.rF, e.wF], [-h, e.track / 2, e.rR, e.wR], [-h, -e.track / 2, e.rR, e.wR]]) {
      const out = Math.sign(y);
      parts.push(disc([x, y, r], r, w, COL.tyre));
      // Sidewall bulge, then the rim: wire wheels on the old cars, big
      // low-profile rims (and covers) on today's.
      parts.push(disc([x, y, r], r * 0.97, w + 0.012, COL.tyre));
      const rim = e.lowProfile ? 0.74 : e.kind === "cigar" ? 0.7 : 0.6;
      parts.push(disc([x, y + out * 0.008, r], r * rim, w + 0.02, e.kind === "cigar" ? COL.wire : e.lowProfile ? COL.carbon : COL.rim));
      parts.push(disc([x, y + out * 0.012, r], r * 0.16, w + 0.03, COL.metal));
    }
  }
  // Wishbones from the body to each wheel.
  function arms(e, parts, bodyHalf, color) {
    const h = e.wb / 2;
    for (const [x, r] of [[h, e.rF], [-h, e.rR]]) for (const sy of [1, -1]) {
      const wy = sy * (e.track / 2 - 0.12);
      parts.push(rod([x + 0.18, sy * bodyHalf, r + 0.05], [x, wy, r + 0.06], 0.018, color));
      parts.push(rod([x - 0.18, sy * bodyHalf, r + 0.05], [x, wy, r + 0.06], 0.018, color));
      parts.push(rod([x + 0.15, sy * bodyHalf, r - 0.1], [x, wy, r - 0.1], 0.018, color));
      parts.push(rod([x - 0.15, sy * bodyHalf, r - 0.1], [x, wy, r - 0.1], 0.018, color));
    }
  }

  function carParts(lap) {
    const e = eraOf(lap.year), L = liveryOf(lap), h = e.wb / 2, parts = [];
    wheels(e, parts);
    if (e.kind === "cigar") {
      // 1950s – 60s: a slim cigar, open wheels on wire spokes.
      parts.push(blob([0.05, 0, 0.42], [h + 0.62, 0.34, 0.27], L.body));
      parts.push(blob([h + 0.62, 0, 0.4], [0.07, 0.17, 0.14], COL.carbon));                 // radiator grille
      if (L.band) parts.push(blob([h + 0.4, 0, 0.42], [0.16, 0.2, 0.17], L.nose));           // nose band
      if (L.stripes) for (const sy of [0.07, -0.07]) parts.push(box([0.35, sy, 0.69], [2.1, 0.05, 0.012], L.accent));
      parts.push(box([0.15, 0, 0.72], [0.04, 0.36, 0.13], COL.glass));                       // screen
      parts.push(blob([-0.2, 0, 0.82], [0.13, 0.12, 0.13], L.helmet));                      // helmet
      parts.push(rod([-0.42, 0.16, 0.6], [-0.42, 0.16, 0.92], 0.02, COL.metal));             // roll hoop
      parts.push(rod([-0.42, -0.16, 0.6], [-0.42, -0.16, 0.92], 0.02, COL.metal));
      parts.push(rod([-0.42, 0.16, 0.92], [-0.42, -0.16, 0.92], 0.02, COL.metal));
      for (const sy of [0.12, -0.12]) parts.push(rod([-0.5, sy, 0.32], [-h - 0.55, sy * 1.4, 0.36], 0.03, COL.metal, 0.036)); // exhausts
      arms(e, parts, 0.25, COL.metal);
      return parts;
    }
    if (e.kind === "wedge") {
      // 1968 – 77: wedge nose, side radiators, wings on tall struts.
      parts.push(box([0.05, 0, 0.36], [e.wb * 0.85, 0.56, 0.34], L.body));                   // tub
      parts.push(cone(0.45 * e.wb, h + 0.72, 0.4, 0.2, 0.25, 0.06, L.body, 1.6, 0.7));        // wedge nose
      if (L.band) parts.push(cone(h + 0.3, h + 0.5, 0.29, 0.25, 0.17, 0.14, L.nose, 1.6, 0.75));
      for (const sy of [1, -1]) parts.push(box([-0.15, sy * 0.44, 0.3], [1.3, 0.34, 0.3], L.body)); // radiator pods
      parts.push(blob([-0.55, 0, 0.5], [0.75, 0.26, 0.2], L.body));                         // engine cover
      parts.push(box([-h - 0.1, 0, 0.32], [0.55, 0.5, 0.3], COL.metal));                    // gearbox / engine
      if (L.stripes || L.pin) for (const sy of L.pin ? [0.2, -0.2] : [0.06, -0.06]) parts.push(box([0.3, sy, 0.54], [2.0, L.pin ? 0.02 : 0.05, 0.012], L.pin ? L.gold : L.accent));
      if (L.gold && !L.pin) parts.push(box([0.3, 0, 0.545], [2.0, 0.025, 0.014], L.gold));
      parts.push(box([0.42, 0, 0.62], [0.05, 0.42, 0.12], COL.glass));
      parts.push(blob([0.12, 0, 0.72], [0.14, 0.13, 0.14], L.helmet));
      if (e.airbox) parts.push(blob([-0.2, 0, 0.9], [0.32, 0.17, 0.16], L.body, pitch(-6)));
      parts.push(box([h + 0.7, 0, 0.2], [0.32, 1.25, 0.025], L.wing));                       // nose fins
      if (e.rearWing) {
        parts.push(box([-h - 0.55, 0, 1.0], [0.42, 1.0, 0.035], L.wing));
        for (const sy of [0.5, -0.5]) parts.push(box([-h - 0.55, sy, 0.95], [0.45, 0.02, 0.2], L.accent));
        for (const sy of [0.2, -0.2]) parts.push(rod([-h - 0.45, sy, 0.45], [-h - 0.55, sy, 0.98], 0.02, COL.carbon));
      }
      arms(e, parts, 0.28, COL.carbon);
      return parts;
    }
    // 1978 onwards: monocoque, sidepods, front and rear wings.
    const highZ = e.highNose ? 0.5 : e.noseZ || 0.32, tub = 0.6;
    parts.push(box([-0.1 * e.wb, 0, 0.09], [e.wb + 0.55, e.track * 0.74, 0.04], COL.carbon)); // floor
    parts.push(box([0.17 * e.wb, 0, 0.37], [e.wb * 0.62, tub, 0.4], L.body));               // tub
    parts.push(box([0.17 * e.wb, 0, 0.58], [e.wb * 0.6, tub * 0.75, 0.04], L.top));          // tub top deck
    parts.push(cone(0.45 * e.wb, h + 0.78, highZ + 0.05, e.highNose ? 0.33 : 0.18, 0.27, 0.07, L.body, 1.15, 0.85));
    parts.push(cone(h + 0.45, h + 0.8, e.highNose ? 0.37 : 0.2, e.highNose ? 0.33 : 0.18, 0.11, 0.06, L.nose, 1.15, 0.85)); // nose tip
    if (e.highNose) for (const sy of [0.12, -0.12]) parts.push(box([h + 0.6, sy, 0.22], [0.25, 0.03, 0.2], COL.carbon)); // wing pylons
    // Sidepods: long and flat in the turbo years, rounder later.
    const podY = tub / 2 + (e.kind === "turbo" ? 0.3 : 0.22), podL = e.kind === "turbo" ? 1.9 : 1.55;
    for (const sy of [1, -1]) {
      parts.push(box([-0.12 * e.wb, sy * podY, 0.26], [podL * 0.8, e.kind === "turbo" ? 0.56 : 0.4, e.ground ? 0.3 : 0.26], L.body));
      parts.push(blob([-0.12 * e.wb + podL * 0.4, sy * podY, 0.26], [0.2, e.kind === "turbo" ? 0.28 : 0.2, e.ground ? 0.15 : 0.13], L.body)); // rounded intake
      parts.push(box([-0.12 * e.wb, sy * (podY + 0.01), 0.17], [podL * 0.8 + 0.01, e.kind === "turbo" ? 0.56 : 0.4, 0.1], L.lower));
      parts.push(box([0.17 * e.wb, sy * (tub / 2 + 0.22), 0.6], [0.08, 0.1, 0.05], L.body)); // mirror
      parts.push(rod([0.17 * e.wb, sy * (tub / 2), 0.55], [0.17 * e.wb, sy * (tub / 2 + 0.2), 0.6], 0.012, COL.carbon));
      if (e.aero) parts.push(box([0.2 * e.wb, sy * (tub / 2 + 0.15), 0.3], [0.5, 0.02, 0.3], COL.carbon)); // bargeboards
    }
    // Cockpit, helmet, engine cover and airbox.
    parts.push(blob([-0.02 * e.wb, 0, 0.68], [0.14, 0.13, 0.14], L.helmet));
    parts.push(box([-0.02 * e.wb + 0.05, 0, 0.6], [0.5, 0.36, 0.06], COL.carbon));            // cockpit rim
    parts.push(blob([-0.32 * e.wb, 0, 0.45], [0.95, 0.25, e.airbox ? 0.27 : 0.22], L.body));
    if (e.airbox) parts.push(blob([-0.13 * e.wb, 0, 0.74], [0.3, 0.12, 0.13], L.top));
    if (e.airbox) parts.push(box([-0.13 * e.wb + 0.12, 0, 0.77], [0.05, 0.11, 0.08], COL.carbon)); // intake
    if (e.fin) parts.push(box([-0.42 * e.wb, 0, 0.78], [1.0, 0.02, 0.22], L.body));
    // Stripes along the top.
    if (L.pin) for (const sy of [0.2, -0.2]) parts.push(box([0.17 * e.wb, sy, 0.605], [e.wb * 0.6, 0.015, 0.008], L.gold));
    parts.push(box([0.3 * e.wb, 0, 0.606], [e.wb * 0.45, 0.06, 0.006], L.accent));
    // Front wing: narrow on older cars, the full width since 2009.
    const fw = e.wideWing ? e.track + e.wF * 0.6 : e.track - 0.15;
    parts.push(box([h + 0.68, 0, 0.1], [0.5, fw, 0.03], L.wing));
    parts.push(box([h + 0.58, 0, 0.17], [0.22, fw * (e.ground ? 0.92 : 0.95), 0.025], L.wing, pitch(-14)));
    if (e.wideWing || e.aero) parts.push(box([h + 0.52, 0, 0.23], [0.15, fw * 0.9, 0.02], L.accent, pitch(-22)));
    for (const sy of [fw / 2, -fw / 2]) parts.push(box([h + 0.65, sy, 0.16], [0.55, 0.02, 0.17], L.accent));
    // Rear wing with endplates, a beam wing below, and the diffuser.
    const rx = -h - 0.42, rwW = e.kind === "turbo" ? 1.05 : e.wideWing ? 0.95 : 1.0;
    const rwZ = e.kind === "turbo" ? 0.9 : e.wideWing && !e.ground ? 0.92 : 0.86;
    parts.push(box([rx, 0, rwZ], [0.34, rwW, 0.045], L.wing, pitch(-8)));
    parts.push(box([rx - 0.12, 0, rwZ + 0.12], [0.2, rwW, 0.03], L.wing, pitch(-26)));
    for (const sy of [rwW / 2, -rwW / 2]) parts.push(box([rx - 0.03, sy, rwZ - 0.14], [0.62, 0.025, e.ground ? 0.42 : 0.55], L.accent));
    parts.push(box([rx + 0.18, 0, 0.42], [0.14, 0.06, rwZ - 0.4], COL.carbon));             // wing pillar
    parts.push(box([rx + 0.05, 0, 0.35], [0.2, rwW * 0.7, 0.025], COL.carbon));             // beam wing
    parts.push(box([-h - 0.05, 0, 0.16], [0.6, e.track * 0.62, 0.12], COL.carbon, pitch(10))); // diffuser
    parts.push(box([-h - 0.2, 0, 0.3], [0.4, 0.4, 0.22], COL.carbon));                      // gearbox
    if (e.halo) {
      const hx = 0.17 * e.wb + 0.3, top = 0.88, back = -0.02 * e.wb - 0.12;
      parts.push(rod([hx + 0.06, 0, 0.62], [hx - 0.04, 0, top], 0.025, COL.carbon));
      for (const sy of [1, -1]) {
        parts.push(rod([hx - 0.04, 0, top], [hx - 0.28, sy * 0.25, top - 0.02], 0.023, COL.carbon));
        parts.push(rod([hx - 0.28, sy * 0.25, top - 0.02], [back, sy * 0.26, top - 0.06], 0.023, COL.carbon));
        parts.push(rod([back, sy * 0.26, top - 0.06], [back - 0.05, sy * 0.27, 0.6], 0.023, COL.carbon));
      }
    }
    arms(e, parts, tub / 2, COL.carbon);
    return parts;
  }
  const cars = {};
  function makeCar(lap) {
    if (cars[lap.id]) return cars[lap.id];
    const e = eraOf(lap.year);
    const body = scene.primitives.add(new Cesium.Primitive({
      geometryInstances: carParts(lap),
      appearance: new Cesium.PerInstanceColorAppearance({ flat: false, translucent: false }),
      asynchronous: false, show: false, shadows: Cesium.ShadowMode.DISABLED,
    }));
    // A soft dark patch under the car, so it sits on the road.
    const shadow = scene.primitives.add(new Cesium.Primitive({
      geometryInstances: blob([-0.1, 0, 0.02], [e.len / 2 + 0.2, e.track / 2 + 0.35, 0.012], Cesium.Color.BLACK.withAlpha(0.42)),
      appearance: new Cesium.PerInstanceColorAppearance({ flat: true, translucent: true }),
      asynchronous: false, show: false, shadows: Cesium.ShadowMode.DISABLED,
    }));
    return (cars[lap.id] = { body, shadow, len: e.len });
  }
  let car = null;
  const showCar = (on) => { for (const c of Object.values(cars)) { c.body.show = on && c === car; c.shadow.show = on && c === car; } };

  // ---- Sky: one steady summer sky -------------------------------------------
  // Down at street level Cesium's sky turned black or changed colour: its
  // atmosphere is worked out for a view from above, goes dark below the
  // horizon (normally hidden by the globe, which the photoreal view turns
  // off) and shifts tint with height and the angle to the sun, so every
  // bump, turn or missing far tile showed. During the laps it is replaced by
  // a dome 30 km wide that travels with the camera: deep blue overhead, pale
  // at the horizon, sea blue below it, and a soft glow around the sun.
  const SKY_R = 30000;
  const skyDome = new Cesium.Primitive({
    geometryInstances: new Cesium.GeometryInstance({
      geometry: new Cesium.EllipsoidGeometry({
        radii: new Cesium.Cartesian3(SKY_R, SKY_R, SKY_R), stackPartitions: 32, slicePartitions: 48,
        vertexFormat: Cesium.MaterialAppearance.MaterialSupport.BASIC.vertexFormat,
      }),
    }),
    appearance: new Cesium.MaterialAppearance({
      materialSupport: Cesium.MaterialAppearance.MaterialSupport.BASIC,
      flat: true, faceForward: true, closed: false, translucent: false,
      material: new Cesium.Material({
        translucent: false,
        fabric: {
          uniforms: { night: 0.0, moonDir: new Cesium.Cartesian3(0, 0, 1) },
          source: `
            czm_material czm_getMaterial(czm_materialInput materialInput) {
              czm_material m = czm_getDefaultMaterial(materialInput);
              vec3 dir = normalize(-materialInput.positionToEyeEC);
              vec3 up = normalize(czm_viewRotation * normalize(czm_viewerPositionWC));
              float e = dot(dir, up);
              vec3 zenith = mix(vec3(0.24, 0.47, 0.80), vec3(0.015, 0.025, 0.07), night);
              vec3 horizon = mix(vec3(0.80, 0.88, 0.95), vec3(0.16, 0.13, 0.17), night);
              vec3 sea = mix(vec3(0.12, 0.31, 0.47), vec3(0.02, 0.03, 0.06), night);
              vec3 col = e >= 0.0
                ? mix(horizon, zenith, pow(smoothstep(0.0, 0.5, e), 0.5))
                : mix(horizon * 0.9, sea, smoothstep(0.0, 0.06, -e));
              float s = max(dot(dir, normalize(czm_sunDirectionEC)), 0.0);
              col += (1.0 - night) * vec3(1.0, 0.95, 0.85) * (0.55 * pow(s, 900.0) + 0.12 * pow(s, 12.0));
              // Night: stars, and a moon low in the south-east.
              if (night > 0.0 && e > 0.02) {
                vec3 dw = normalize((czm_inverseViewRotation * dir));
                vec3 q = floor(dw * 420.0);
                float h = fract(sin(dot(q, vec3(12.9898, 78.233, 37.719))) * 43758.5453);
                col += night * vec3(0.9, 0.92, 1.0) * step(0.9975, h) * smoothstep(0.02, 0.2, e);
                vec3 moon = normalize(czm_viewRotation * moonDir);
                float mm = max(dot(dir, moon), 0.0);
                col += night * (vec3(0.95, 0.95, 0.9) * smoothstep(0.99965, 0.9998, mm) + vec3(0.25, 0.3, 0.45) * pow(mm, 30.0) * 0.5);
              }
              m.diffuse = vec3(0.0);
              m.emission = col;
              return m;
            }`,
        },
      }),
    }),
    asynchronous: false, show: false, shadows: Cesium.ShadowMode.DISABLED,
  });
  scene.primitives.add(skyDome);
  let oldSky = null;
  function setSky(on) {
    if (on && !oldSky) {
      oldSky = true;
      skyDome.show = true;
    } else if (!on && oldSky) {
      skyDome.show = false;
      oldSky = null;
    }
  }
  scene.preRender.addEventListener(() => {
    if (skyDome.show) skyDome.modelMatrix = Cesium.Matrix4.fromTranslation(cam.positionWC, skyDome.modelMatrix);
  });

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

  // Phones start with the high TV camera, which streams far more smoothly;
  // ?cam=chase, tv or onboard picks one.
  const CAM = new URLSearchParams(location.search).get("cam");
  let view = ["chase", "tv", "onboard"].includes(CAM) ? CAM
    : (matchMedia("(pointer: coarse)").matches && Math.min(screen.width, screen.height) < 700 ? "tv" : "chase");
  const zUp = new Cesium.Cartesian3(0, 0, 1);
  const dir = new Cesium.Cartesian3(), up = new Cesium.Cartesian3(), right = new Cesium.Cartesian3(), eye = new Cesium.Cartesian3();
  function cameraFor(u, v) {
    let ex, ey, ez, tx, ty, tz;
    if (view === "chase") {
      const back = 8.5 + 0.035 * v, uc = u - back / DS, ut = u + 2.5 / DS;
      ex = lerp(QX, uc); ey = lerp(QY, uc); ez = Math.max(lerp(CZ, uc), lerp(CZ, u)) + zOff + 2.6;
      tx = lerp(CX, ut); ty = lerp(CY, ut); tz = lerp(CZ, ut) + zOff + 0.75;
    } else if (view === "tv") {
      // A high camera following from further back, like the TV pictures:
      // it needs far less fine detail right next to it than the low chase
      // view, so it streams much more smoothly. Low again in the tunnel.
      const back = 17 + 0.06 * v, uc = u - back / DS, ut = u + 4 / DS;
      const inTunnel = K.tunnelWeight(((uc % N) + N) % N * DS, 25, 10);
      ex = lerp(QX, uc); ey = lerp(QY, uc);
      ez = Math.max(lerp(CZ, uc), lerp(CZ, u)) + zOff + 9 * (1 - inTunnel) + 3.6 * inTunnel;
      tx = lerp(CX, ut); ty = lerp(CY, ut); tz = lerp(CZ, ut) + zOff + 0.3;
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
    const lapPlaying = state === "playing";
    document.body.classList.toggle("lap-playing", lapPlaying);
    if (!lapPlaying && M.setTourSpeed) M.setTourSpeed(0);
    if (state === "off" || state === "pick" && !lap) return;
    if (state === "paused" && !looked && !flying) applyCamera();
    if ((state === "playing" || state === "intro" || flying) && ctrl.enableInputs) ctrl.enableInputs = false;
    if (state !== "playing") return;
    const now = performance.now();
    tl += Math.min(0.5, (now - last) / 1000);     // real time, so the lap takes the real lap time
    last = now;
    if (tl >= tPost) { tl = tPost; draw(); applyCamera(); finish(); return; }
    // Report the speed so the scenery detail can follow it (main.js).
    if (M.setTourSpeed) { const u = uAt(tl); M.setTourSpeed(speedAt(tl, u)); }
    draw();
    applyCamera();
  }
  scene.preUpdate.addEventListener(tick);

  function setFov() {
    const f = cam.frustum;
    if (!(f instanceof Cesium.PerspectiveFrustum)) return;
    if (oldFov === null) oldFov = f.fov;
    const c = scene.canvas, portrait = c.clientHeight > c.clientWidth;
    const deg = view === "onboard" ? (portrait ? 80 : 68) : (portrait ? 72 : 60);
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

  // The camera button cycles chase → TV → onboard, and shows the next one.
  const VIEWS = ["chase", "tv", "onboard"];
  const nextView = () => VIEWS[(VIEWS.indexOf(view) + 1) % VIEWS.length];
  function labelViewButton() {
    const n = nextView();
    hud.buttons[0].innerHTML = n === "onboard" ? ICON_ONBOARD : n === "tv" ? ICON_TV : ICON_CHASE;
    hud.buttons[0].title = n === "onboard" ? "Onboard camera" : n === "tv" ? "TV camera (smoothest)" : "Chase camera";
  }
  function switchView(to) {
    view = to || nextView();
    labelViewButton();
    showCar(view !== "onboard" && state !== "off");
    setFov();
    if (state !== "playing") { looked = false; applyCamera(); }
  }
  const ICON_ONBOARD = '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="10" cy="10" r="7.2"/><circle cx="10" cy="10" r="1.6" fill="currentColor"/><path d="M3 10h5.4M11.6 10H17M10 11.6V17"/></svg>';
  const ICON_CHASE = '<svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor"><path d="M3 12.5h14v2.2H3zM6 9.5h8l1.2 3H4.8zM8.6 7.4h2.8v2.2H8.6z"/><circle cx="5.2" cy="15" r="1.7"/><circle cx="14.8" cy="15" r="1.7"/></svg>';
  const ICON_TV = '<svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor"><rect x="2.5" y="6" width="10.5" height="8" rx="1.6"/><path d="M13.8 9.2 17.5 7v6l-3.7-2.2z"/></svg>';
  const ICON_LIST = '<svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor"><rect x="3" y="4" width="14" height="2.2" rx="1"/><rect x="3" y="9" width="14" height="2.2" rx="1"/><rect x="3" y="14" width="14" height="2.2" rx="1"/></svg>';

  // ---- Day and night ----------------------------------------------------
  // One switch for the whole site: the film set keeps the day/night state
  // (filmset.js, its "☾ Night" button), and this button flips the same one.
  // The film set lights its own night (windows, lamps, moon). On the other
  // scenery the laps add their own: a night sky with stars and moon, the
  // Google scan and the modelled track darkened to moonlight with a warm
  // pool of street light near the camera, and moonlight on the car.
  const ICON_MOON = '<svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor"><path d="M12.9 3.2a7 7 0 1 0 3.9 11.9A6 6 0 0 1 12.9 3.2z"/></svg>';
  const ICON_SUN = '<svg width="20" height="20" viewBox="0 0 20 20" fill="currentColor"><circle cx="10" cy="10" r="3.6"/><g stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M10 1.8v2.2M10 16v2.2M1.8 10h2.2M16 10h2.2M4.2 4.2l1.6 1.6M14.2 14.2l1.6 1.6M4.2 15.8l1.6-1.6M14.2 5.8l1.6-1.6"/></g></svg>';
  let localNight = new URLSearchParams(location.search).get("night") === "1";
  const isNight = () => (M.filmset && M.filmset.isNight ? M.filmset.isNight() : localNight);
  const filmOn = () => !!(M.filmset && M.filmset.isOn && M.filmset.isOn());
  function moonDir() {             // same moon as the film set
    const az = Cesium.Math.toRadians(150), el = Cesium.Math.toRadians(24);
    const toMoon = new Cesium.Cartesian3(Math.cos(el) * Math.sin(az), Math.cos(el) * Math.cos(az), Math.sin(el));
    const enu = Cesium.Transforms.eastNorthUpToFixedFrame(Cesium.Cartesian3.fromDegrees(7.428, 43.7395));
    const d = Cesium.Matrix4.multiplyByPointAsVector(enu, toMoon, new Cesium.Cartesian3());
    return Cesium.Cartesian3.normalize(d, d);
  }
  const GOOGLE_NIGHT = new Cesium.CustomShader({
    fragmentShaderText: `
      void fragmentMain(FragmentInput fsInput, inout czm_modelMaterial material) {
        float near = exp(-length(fsInput.attributes.positionEC) / 28.0);
        vec3 c = material.diffuse;
        material.diffuse = c * vec3(0.16, 0.19, 0.3) + c * vec3(1.0, 0.78, 0.5) * 0.5 * near;
      }`,
  });
  let nightShown = null, savedLight = null, nightGoogle = null;
  function showNight() {
    const n = state !== "off" && isNight();
    const film = filmOn();
    const btn = hud.buttons && hud.buttons[NIGHT_BTN];
    if (state !== "off" && btn && btn.dataset.night !== String(n)) {
      btn.dataset.night = String(n);
      btn.innerHTML = n ? ICON_SUN : ICON_MOON;
      btn.title = n ? "Day" : "Night";
      btn.setAttribute("aria-label", btn.title);
    }
    const key = `${n}|${film}`;
    if (key === nightShown) return;
    nightShown = key;
    const v = n ? 1 : 0;
    const u = skyDome.appearance.material.uniforms;
    u.night = v; u.moonDir = moonDir();
    if (M.cleanTrack && M.cleanTrack.setNight) M.cleanTrack.setNight(n && !film ? 1 : 0);
    // Google's scan: darkened only while the laps run at night.
    if (nightGoogle && nightGoogle.customShader === GOOGLE_NIGHT) nightGoogle.customShader = undefined;
    nightGoogle = null;
    const g = M.getGoogle && M.getGoogle();
    if (n && g && !film) { g.customShader = GOOGLE_NIGHT; nightGoogle = g; }
    // Moonlight on the car (the film set sets its own light).
    if (n && !film) {
      if (!savedLight) savedLight = scene.light;
      scene.light = new Cesium.DirectionalLight({ direction: Cesium.Cartesian3.negate(moonDir(), new Cesium.Cartesian3()), color: new Cesium.Color(0.6, 0.7, 1.0), intensity: 0.65 });
    } else if (savedLight) {
      if (!film) scene.light = savedLight;
      savedLight = null;
    }
  }
  function toggleNight() {
    const on = !isNight();
    if (M.filmset && M.filmset.setNight) M.filmset.setNight(on); else localNight = on;
    nightShown = null;
    showNight();
  }
  scene.preRender.addEventListener(() => { if (state !== "off" || nightShown) showNight(); });
  const NIGHT_BTN = 2;

  const handlers = {
    onToggle: toggle, onSeek: seek, onReplay: () => begin(lap),
    marks: K.CORNERS.slice(1).map((c) => c.s / LEN),
    buttons: [
      { html: ICON_ONBOARD, title: "Onboard camera", onClick: () => switchView() },
      { html: ICON_LIST, title: "Choose another lap", onClick: () => openPicker() },
      { html: ICON_MOON, title: "Night", onClick: () => toggleNight() },
    ].concat(M.streetView && M.streetView.available && K.svButton
      ? [K.svButton(() => (lap ? uAt(tl) * DS : 0), () => (state === "intro" ? seek(0) : pause(false)))] : []),
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
    setSky(new URLSearchParams(location.search).get("sky") !== "cesium");
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
    car = makeCar(lap);
    showCar(view !== "onboard");
    setFov();
    hud.show(handlers);
    labelViewButton();
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
  const PREP_STEP = 150;          // m between the places the camera visits
  // Each stop waits for its tiles, but never long: the whole preparation
  // stays under about 10 s on a slow connection.
  const PREP_WAIT = 1200;
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
    // Only the first stretch: a phone's tile cache cannot hold the whole
    // circuit at street level, so tiles loaded for the far side of the lap
    // were thrown away again before the car got there. The rest streams
    // in during the lap.
    const stops = [];
    for (let s = 600; s > 0; s -= PREP_STEP) stops.push(s / DS);
    stops.push(-RUNUP / DS, -RUNUP / 2 / DS, 0, 30 / DS);
    for (let k = 0; k < stops.length && !skip; k++) {
      if (mine !== run) break;
      const u = stops[k];
      cam.setView(cameraFor(u, lap.V[wrap(Math.round(u))]));
          let frames = 0;
      while (!skip && mine === run) {
        await frameDone();
        frames++;
        if ((frames > 2 && google.tilesLoaded) || performance.now() - t0 > PREP_WAIT) break;
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
    setSky(false);
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
    LAPS, choose, seek, bake, racingLine: RACE_OFF, setSky, makeCar,
    skyNight: (v, moon) => {
      const u = skyDome.appearance.material.uniforms;
      u.night = v;
      if (moon) u.moonDir = moon;
    },
    get state() { return state; }, get clock() { return tl; }, get lap() { return lap; },
    set view(v) { if (v !== view && VIEWS.includes(v)) switchView(v); },
  };
})();
