// A clean race track for the lap tours.
//
// Google's photoreal mesh is a scan of a normal day in Monaco: parked and
// moving cars, buses and roadworks are baked into the road, and the tunnel
// under the Fairmont hotel has no inside at all. For the laps we:
//   - cut a corridor the width of the road out of the Google mesh (clipping
//     polygons), which removes the cars and works standing on it,
//   - lay a modelled track in its place: asphalt, white edge lines,
//     red-and-white kerbs in the corners, the start line, low barriers,
//   - build the tunnel: walls, ceiling and rows of lights. The hotel is
//     cut away around it, and a stone block fills the cut above the vault
//     so nothing looks sliced from outside.
// Everything is a handful of light primitives built once, on first use.
//
// MC3D.cleanTrack.show(on) turns it on or off. The cut is worked out once
// on the graphics card when first shown, and never changed after, as each
// change costs a noticeable pause on a phone.
// Add ?clean=0 to the address to compare with the raw scan.
/* global Cesium */
(function () {
  const M = window.MC3D, K = M && M.lapKit;
  if (!M || !K) return;
  if (new URLSearchParams(location.search).get("clean") === "0") return;
  const { viewer } = M;
  const scene = viewer.scene;
  const { N, DS, LEN, X, Y, ZG, ZC, wrap, smooth, toWorld, TUN } = K;

  const HALF = 5.0;           // m, half the width of the modelled track
  const CUT = 4.7;            // m, half the width cut out of the scan (under the track)
  const LIFT = 0.1;           // m, the track sits this much above the ground model
  const BARRIER = HALF + 0.35, BARRIER_H = 1.0;
  const [TA, TB] = TUN[0] || [1e9, -1e9];

  // The same smoothed line as the car (lap-legends.js), so it sits on the track.
  const CX = smooth(X, 2), CY = smooth(Y, 2), CZ = smooth(ZG, 3);
  const at = (A, s) => { const u = s / DS, i = Math.floor(u), f = u - i; return A[wrap(i)] * (1 - f) + A[wrap(i + 1)] * f; };
  function frame(s) {
    const x = at(CX, s), y = at(CY, s), z = at(CZ, s) + LIFT;
    let dx = at(CX, s + 1.5) - at(CX, s - 1.5), dy = at(CY, s + 1.5) - at(CY, s - 1.5);
    const l = Math.hypot(dx, dy) || 1;
    dx /= l; dy /= l;
    return { x, y, z, lx: -dy, ly: dx };            // left = up × forward
  }
  // Top of whatever stands over the road (the hotel above the tunnel), from
  // the LiDAR surface model.
  function topAt(s) {
    let t = -1e9;
    for (let d = -6; d <= 6; d += 2) t = Math.max(t, at(ZC, s + d));
    return Math.max(t, at(CZ, s) + 8);
  }
  // Signed curvature (+ = turning left), for the kerbs.
  function turn(s) {
    const h = (q) => Math.atan2(at(CY, q + 2) - at(CY, q - 2), at(CX, q + 2) - at(CX, q - 2));
    return Cesium.Math.negativePiToPi(h(s + 3) - h(s - 3)) / 6;
  }

  const tmp = new Cesium.Cartesian3();
  // A sheet made of rings across the track: rings[k] = { s, pts: [[x, y, z, t]] }.
  function sheet(rings) {
    const n = rings[0].pts.length, pos = [], st = [], nrm = [], idx = [];
    for (const r of rings) {
      for (const [x, y, z, t] of r.pts) {
        Cesium.Matrix4.multiplyByPoint(toWorld, Cesium.Cartesian3.fromElements(x, y, z, tmp), tmp);
        pos.push(tmp.x, tmp.y, tmp.z);
        st.push(r.s, t);
        Cesium.Matrix4.multiplyByPointAsVector(toWorld, Cesium.Cartesian3.UNIT_Z, tmp);
        nrm.push(tmp.x, tmp.y, tmp.z);
      }
    }
    for (let k = 0; k < rings.length - 1; k++) {
      for (let j = 0; j < n - 1; j++) {
        const a = k * n + j, b = a + 1, c = a + n, d = c + 1;
        idx.push(a, c, b, b, c, d);
      }
    }
    const values = new Float64Array(pos);
    return new Cesium.Geometry({
      attributes: {
        position: new Cesium.GeometryAttribute({ componentDatatype: Cesium.ComponentDatatype.DOUBLE, componentsPerAttribute: 3, values }),
        normal: new Cesium.GeometryAttribute({ componentDatatype: Cesium.ComponentDatatype.FLOAT, componentsPerAttribute: 3, values: new Float32Array(nrm) }),
        st: new Cesium.GeometryAttribute({ componentDatatype: Cesium.ComponentDatatype.FLOAT, componentsPerAttribute: 2, values: new Float32Array(st) }),
      },
      indices: values.length / 3 > 65535 ? new Uint32Array(idx) : new Uint16Array(idx),
      primitiveType: Cesium.PrimitiveType.TRIANGLES,
      boundingSphere: Cesium.BoundingSphere.fromVertices(values),
    });
  }
  // st.s is metres along the lap, st.t runs across (0 → 1).
  const mat = (source, uniforms) => new Cesium.MaterialAppearance({
    material: new Cesium.Material({ fabric: { uniforms: uniforms || {}, source } }),
    flat: true, faceForward: false, translucent: false, closed: false,
  });
  const NOISE = `
    float h21(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
    float vnoise(vec2 p) { vec2 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
      return mix(mix(h21(i), h21(i + vec2(1, 0)), f.x), mix(h21(i + vec2(0, 1)), h21(i + vec2(1, 1)), f.x), f.y); }`;
  const ROAD = `${NOISE}
    czm_material czm_getMaterial(czm_materialInput mi) {
      czm_material m = czm_getDefaultMaterial(mi);
      float s = mi.st.s, t = mi.st.t, w = (t - 0.5) * 2.0 * halfW;  // metres from the centre
      vec2 p = vec2(mod(s, 97.0), w);                               // keep sin() precise
      float n = vnoise(p * 3.1) * 0.5 + vnoise(p * 11.0) * 0.3 + vnoise(p * 0.35) * 0.2;
      vec3 c = vec3(0.20, 0.205, 0.215) * (0.82 + 0.32 * n);
      c *= 1.0 - 0.10 * (1.0 - smoothstep(0.0, 1.4, abs(abs(w) - 1.6)));   // the racing line, a touch darker
      float edge = step(halfW - 0.42, abs(w)) * step(abs(w), halfW - 0.18);
      c = mix(c, vec3(0.86), edge);
      float sl = min(s, len - s);                                   // start / finish line
      c = mix(c, vec3(0.92), step(sl, 0.35));
      float inT = smoothstep(tunA - 3.0, tunA + 6.0, s) * (1.0 - smoothstep(tunB - 6.0, tunB + 3.0, s));
      float lamp = 0.75 + 0.25 * (1.0 - smoothstep(0.0, 0.5, abs(fract(s / 2.4) - 0.5)));
      c = mix(c, c * vec3(0.95, 0.82, 0.62) * 0.75 * lamp, inT);
      m.diffuse = c;
      return m;
    }`;
  const KERB = `
    czm_material czm_getMaterial(czm_materialInput mi) {
      czm_material m = czm_getDefaultMaterial(mi);
      float red = step(0.5, fract(mi.st.s / 1.6));
      m.diffuse = mix(vec3(0.93, 0.93, 0.9), vec3(0.78, 0.09, 0.08), red);
      return m;
    }`;
  const BARR = `
    czm_material czm_getMaterial(czm_materialInput mi) {
      czm_material m = czm_getDefaultMaterial(mi);
      float t = mi.st.t;
      vec3 steel = vec3(0.62, 0.64, 0.66) * (0.8 + 0.2 * smoothstep(0.35, 0.6, t));
      steel *= 0.86 + 0.14 * step(0.5, fract(t * 3.0));
      vec3 c = mix(vec3(0.16), steel, step(0.18, t));
      c = mix(c, vec3(0.92), step(0.88, t));
      m.diffuse = c;
      return m;
    }`;
  const STONE = `${NOISE}
    czm_material czm_getMaterial(czm_materialInput mi) {
      czm_material m = czm_getDefaultMaterial(mi);
      vec2 p = vec2(mod(mi.st.s, 97.0) * 0.7, mi.st.t * 9.0);
      vec3 c = vec3(0.72, 0.68, 0.6) * (0.85 + 0.15 * vnoise(p * 3.0));
      c *= 0.9 + 0.1 * step(0.06, fract(p.y));
      m.diffuse = c;
      return m;
    }`;
  const TUNNEL = `
    czm_material czm_getMaterial(czm_materialInput mi) {
      czm_material m = czm_getDefaultMaterial(mi);
      float s = mi.st.s, t = mi.st.t;
      float edge = min(t, 1.0 - t);                       // 0 at the road, 0.5 at the crown
      vec3 c = mix(vec3(0.30, 0.29, 0.28), vec3(0.86, 0.83, 0.76), 1.0 - step(0.23, edge));
      float joint = max(step(0.93, fract(s / 1.2)), step(0.94, fract(t * 18.0)));
      c *= 1.0 - 0.12 * joint;
      c = mix(c, vec3(0.22), 1.0 - step(0.035, edge));    // dark kick band
      float row = 1.0 - smoothstep(0.004, 0.014, abs(edge - 0.40));
      float pulse = 1.0 - smoothstep(0.25, 0.45, abs(fract(s / 2.4) - 0.5));
      m.diffuse = c * vec3(1.0, 0.93, 0.82);
      m.emission = vec3(1.0, 0.85, 0.55) * row * (0.3 + 1.2 * pulse);
      return m;
    }`;

  // ---- Build -----------------------------------------------------------
  let built = null;
  function build() {
    const STEP = 2;
    const prims = [];
    // Track surface.
    const road = [];
    for (let s = 0; s <= LEN + 0.01; s += STEP) {
      const f = frame(s);
      road.push({ s: Math.min(s, LEN), pts: [[f.x + f.lx * HALF, f.y + f.ly * HALF, f.z, 0], [f.x - f.lx * HALF, f.y - f.ly * HALF, f.z, 1]] });
    }
    prims.push(new Cesium.Primitive({
      geometryInstances: new Cesium.GeometryInstance({ geometry: sheet(road) }),
      appearance: mat(ROAD, { tunA: TA, tunB: TB, len: LEN, halfW: HALF }),
      asynchronous: false, compressVertices: false, show: false, shadows: Cesium.ShadowMode.RECEIVE_ONLY,
    }));
    // Kerbs on the inside of the corners.
    const kerbs = [];
    let runK = null;
    const flush = () => { if (runK && runK.length > 3) kerbs.push(runK); runK = null; };
    for (let s = 0; s <= LEN; s += 1) {
      const k = turn(s);
      if (Math.abs(k) > 1 / 45 && (s < TA - 10 || s > TB + 10)) {
        const side = Math.sign(k);               // + = left side is the inside
        if (runK && runK.side !== side) flush();
        if (!runK) { runK = []; runK.side = side; }
        const f = frame(s), o1 = side * (HALF - 0.05), o2 = side * (HALF - 1.0);
        runK.push({ s, pts: [[f.x + f.lx * o1, f.y + f.ly * o1, f.z + 0.03, 0], [f.x + f.lx * o2, f.y + f.ly * o2, f.z + 0.03, 1]] });
      } else flush();
    }
    flush();
    if (kerbs.length) {
      prims.push(new Cesium.Primitive({
        geometryInstances: kerbs.map((r) => new Cesium.GeometryInstance({ geometry: sheet(r) })),
        appearance: mat(KERB), asynchronous: false, compressVertices: false, show: false, shadows: Cesium.ShadowMode.RECEIVE_ONLY,
      }));
    }
    // Barriers on both sides, except in the tunnel, which has walls.
    const barriers = [];
    for (const side of [1, -1]) {
      let runB = [];
      for (let s = 0; s <= LEN + 0.01; s += STEP) {
        if (s > TA - 2 && s < TB + 2) { if (runB.length > 1) barriers.push(runB); runB = []; continue; }
        const f = frame(s), o = side * BARRIER;
        const x = f.x + f.lx * o, y = f.y + f.ly * o;
        runB.push({ s, pts: [[x, y, f.z - 0.4, 0], [x, y, f.z + BARRIER_H, 1]] });
      }
      if (runB.length > 1) barriers.push(runB);
    }
    prims.push(new Cesium.Primitive({
      geometryInstances: barriers.map((r) => new Cesium.GeometryInstance({ geometry: sheet(r) })),
      appearance: mat(BARR), asynchronous: false, compressVertices: false, show: false, shadows: Cesium.ShadowMode.DISABLED,
    }));
    // The tunnel: a closed vault from portal to portal.
    if (TA < TB) {
      const PROFILE = [[-6.3, -0.6], [-6.3, 4.4], [-5.2, 5.4], [-2.6, 5.9], [0, 6.0], [2.6, 5.9], [5.2, 5.4], [6.3, 4.4], [6.3, -0.6]];
      let acc = 0;
      const tt = [0];
      for (let j = 1; j < PROFILE.length; j++) { acc += Math.hypot(PROFILE[j][0] - PROFILE[j - 1][0], PROFILE[j][1] - PROFILE[j - 1][1]); tt.push(acc); }
      const vault = [];
      for (let s = TA - 4; s <= TB + 4; s += 1.5) {
        const f = frame(s);
        // +y in PROFILE is to the left.
        vault.push({ s, pts: PROFILE.map(([w, h], j) => [f.x + f.lx * w, f.y + f.ly * w, f.z + h, tt[j] / acc]) });
      }
      prims.push(new Cesium.Primitive({
        geometryInstances: new Cesium.GeometryInstance({ geometry: sheet(vault) }),
        appearance: mat(TUNNEL), asynchronous: false, compressVertices: false, show: false, shadows: Cesium.ShadowMode.DISABLED,
      }));
      // The tunnel is cut out of the scan from end to end (its hotel would
      // otherwise poke into the vault). A stone block fills the cut above
      // the vault up to the height of the hotel, so seen from outside there
      // is no slot through the building, and a wall closes each mouth.
      const block = [];
      for (let s = TA - 4; s <= TB + 4; s += 2) {
        const f = frame(s), top = topAt(s), W = 6.9;
        block.push({ s, pts: [[f.x + f.lx * W, f.y + f.ly * W, f.z + 5.2, 0], [f.x + f.lx * W, f.y + f.ly * W, top, 0.4],
          [f.x - f.lx * W, f.y - f.ly * W, top, 0.6], [f.x - f.lx * W, f.y - f.ly * W, f.z + 5.2, 1]] });
      }
      const walls = [sheet(block)];
      for (const s of [TA - 4, TB + 4]) {
        const f = frame(s), z = f.z, top = topAt(s) - z;
        const q = (w0, w1, h0, h1) => sheet([
          { s: w0, pts: [[f.x + f.lx * w0, f.y + f.ly * w0, z + h0, 0], [f.x + f.lx * w0, f.y + f.ly * w0, z + h1, 1]] },
          { s: w1, pts: [[f.x + f.lx * w1, f.y + f.ly * w1, z + h0, 0], [f.x + f.lx * w1, f.y + f.ly * w1, z + h1, 1]] },
        ]);
        walls.push(q(6.3, 10, -0.6, top), q(-10, -6.3, -0.6, top), q(-6.3, 6.3, 5.3, top));
      }
      prims.push(new Cesium.Primitive({
        geometryInstances: walls.map((g) => new Cesium.GeometryInstance({ geometry: g })),
        appearance: mat(STONE), asynchronous: false, compressVertices: false, show: false, shadows: Cesium.ShadowMode.DISABLED,
      }));
    }
    prims.forEach((p) => scene.primitives.add(p));

    // Corridors cut out of the Google mesh: the road in pieces of about
    // 240 m (a polygon must not loop round on itself), leaving out the
    // tunnel, which has its own, wider one.
    const corridor = (a, b, half) => {
      const left = [], right = [];
      const n = Math.max(2, Math.ceil((b - a) / 6));
      for (let k = 0; k <= n; k++) {
        const s = a + (b - a) * k / n;
        const f = frame(s);
        left.push([f.x + f.lx * half, f.y + f.ly * half]);
        right.push([f.x - f.lx * half, f.y - f.ly * half]);
      }
      const ring = left.concat(right.reverse()).map(([x, y]) =>
        Cesium.Matrix4.multiplyByPoint(toWorld, new Cesium.Cartesian3(x, y, 0), new Cesium.Cartesian3()));
      return new Cesium.ClippingPolygon({ positions: ring });
    };
    const cuts = [];
    const pieces = TA < TB ? [[TB + 4, LEN], [0, TA - 4]] : [[0, LEN]];
    for (const [a, b] of pieces) {
      for (let s = a; s < b; s += 240) cuts.push(corridor(Math.max(a, s - 3), Math.min(b, s + 243), CUT));
    }
    // The tunnel, end to end, a little narrower than the block over it.
    if (TA < TB) cuts.push(corridor(TA - 5, TB + 5, 6.7));
    built = { prims, cuts };
    return built;
  }

  // ---- Showing ---------------------------------------------------------
  let on = false, clip = null, clipOwner = null;
  function attachClipping(google) {
    if (!google || !Cesium.ClippingPolygonCollection || !Cesium.ClippingPolygonCollection.isSupported(scene)) return;
    if (clipOwner === google && clip) { clip.enabled = true; return; }
    clip = new Cesium.ClippingPolygonCollection({ polygons: built.cuts.slice() });
    google.clippingPolygons = clip;
    clipOwner = google;
  }
  function show(yes) {
    on = !!yes;
    if (on && !built) build();
    if (!built) return;
    built.prims.forEach((p) => { p.show = on; });
    const google = M.getGoogle && M.getGoogle();
    if (on) attachClipping(google);
    else if (clip) clip.enabled = false;
  }

  M.cleanTrack = {
    show, lift: LIFT,
    get on() { return on; },
    get primitives() { return built ? built.prims : []; },
    get clipped() { return !!clip && clip.enabled; },
  };
})();
