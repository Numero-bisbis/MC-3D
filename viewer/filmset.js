// Film set: our own stylised model of Monte-Carlo, built only from open data
// (IGN LiDAR HD, IGN BD TOPO, OpenStreetMap) by pipeline/build_filmset.py.
// No Google tiles: one file loads once and then runs from memory, like a set
// for an animated film.
//
// Open it with the "Film set" button in the View group, or ?mode=film.
(function () {
  const M = window.MC3D;
  if (!M || !window.Cesium) return;
  const { viewer } = M;
  const scene = viewer.scene;
  const MOBILE = window.matchMedia("(pointer: coarse)").matches || window.innerWidth < 700;
  const URL_BASE = "filmset/monte-carlo";

  // Kinds written by the pipeline into the colour alpha: kind * 16 + 8.
  // 0 ground, 1 classic wall, 2 modern wall, 3 flat roof, 4 tiled roof,
  // 5 leaves, 6 wood, 7 boat, 8 sea, 9 plain.
  const shader = new Cesium.CustomShader({
    lightingModel: Cesium.LightingModel.PBR,
    uniforms: {
      u_time: { type: Cesium.UniformType.FLOAT, value: 0 },
    },
    fragmentShaderText: `
      float hash12(vec2 p) {
        vec3 p3 = fract(vec3(p.xyx) * 0.1031);
        p3 += dot(p3, p3.yzx + 33.33);
        return fract((p3.x + p3.y) * p3.z);
      }
      float box(vec2 f, vec2 lo, vec2 hi, vec2 aa) {
        vec2 a = smoothstep(lo - aa, lo + aa, f) * (1.0 - smoothstep(hi - aa, hi + aa, f));
        return a.x * a.y;
      }
      void fragmentMain(FragmentInput fsInput, inout czm_modelMaterial material) {
        vec4 c = fsInput.attributes.color_0;
        float kind = floor(c.a * 255.0 / 16.0);
        if (kind < 0.5) {               // ground: the painted map
          material.roughness = 0.95;
          return;
        }
        vec3 base = czm_srgbToLinear(c.rgb);
        vec2 uv = fsInput.attributes.texCoord_0;
        material.roughness = 0.85;
        material.specular = vec3(0.03);
        if (kind < 2.5 || kind > 9.5) {  // walls: windows, floors, shutters
          bool classic = kind < 1.5;
          vec2 f = fract(uv);
          vec2 cell = floor(uv);
          vec2 aa = max(fwidth(uv) * 1.2, vec2(0.002));
          // Fade the pattern out far away instead of letting it shimmer.
          float detail = 1.0 - smoothstep(0.18, 0.45, max(aa.x, aa.y));
          float r = hash12(cell + floor(uv.x * 0.0) + 17.0);
          vec3 col = base;
          vec3 glass = mix(vec3(0.05, 0.07, 0.09), vec3(0.20, 0.26, 0.32), smoothstep(0.3, 1.0, f.y));
          float gnd = 1.0 - step(1.0, uv.y);
          if (uv.y < 0.0) {
            col = base * 0.7;            // plinth below street level
          } else if (classic) {
            float sill = box(f, vec2(0.0, 0.0), vec2(1.0, 0.06), aa);
            col = mix(col, base * 1.08, sill);
            if (gnd > 0.5) {
              float shop = box(f, vec2(0.12, 0.08), vec2(0.88, 0.78), aa);
              float awning = box(f, vec2(0.08, 0.78), vec2(0.92, 0.9), aa);
              vec3 aw = r < 0.33 ? vec3(0.55, 0.12, 0.10) : (r < 0.66 ? vec3(0.13, 0.25, 0.20) : vec3(0.75, 0.70, 0.60));
              col = mix(col, glass * 1.4, shop);
              col = mix(col, aw, awning * step(0.25, r));
            } else {
              float win = box(f, vec2(0.36, 0.22), vec2(0.64, 0.84), aa);
              float frame = box(f, vec2(0.32, 0.18), vec2(0.68, 0.88), aa);
              float shut = box(f, vec2(0.17, 0.2), vec2(0.35, 0.86), aa) + box(f, vec2(0.65, 0.2), vec2(0.83, 0.86), aa);
              vec3 green = mix(vec3(0.20, 0.30, 0.22), vec3(0.42, 0.45, 0.36), hash12(cell * 1.7));
              bool closed = r < 0.18;
              col = mix(col, base * 1.12, frame);
              col = mix(col, closed ? green * 0.9 : glass, win);
              col = mix(col, green, shut * (closed ? 0.0 : 1.0));
              // little balcony rail on some floors
              float rail = box(f, vec2(0.28, 0.2), vec2(0.72, 0.27), aa) * step(0.6, hash12(vec2(cell.y, 3.0)));
              col = mix(col, vec3(0.12), rail * 0.8);
              material.roughness = mix(0.85, 0.3, win * (closed ? 0.0 : 1.0));
            }
          } else if (kind > 9.5) {
            // Glass tower: bands of glass between slab edges.
            float slab = 1.0 - box(f, vec2(-1.0, 0.16), vec2(2.0, 0.92), aa);
            float mull = 1.0 - box(f, vec2(0.04, -1.0), vec2(0.96, 2.0), aa);
            vec3 g2 = mix(vec3(0.12, 0.17, 0.22), vec3(0.40, 0.52, 0.60), smoothstep(0.1, 1.0, f.y));
            g2 *= 0.85 + 0.3 * hash12(cell);
            col = mix(g2, base, clamp(slab + mull * 0.8, 0.0, 1.0));
            material.roughness = mix(0.15, 0.8, clamp(slab + mull, 0.0, 1.0));
            material.specular = mix(vec3(0.08), vec3(0.03), clamp(slab + mull, 0.0, 1.0));
          } else {
            // Riviera residential: white slab edges, balcony rails, deep
            // windows behind, a solid pier every few bays.
            float slab = 1.0 - box(f, vec2(-1.0, 0.10), vec2(2.0, 1.1), aa);
            float rail = box(f, vec2(-1.0, 0.10), vec2(2.0, 0.36), aa);
            float pier = 1.0 - box(f, vec2(0.07, -1.0), vec2(0.93, 2.0), aa);
            float win = box(f, vec2(0.14, 0.36), vec2(0.86, 0.95), aa);
            vec3 shade = base * 0.55;
            vec3 g2 = mix(vec3(0.10, 0.13, 0.16), vec3(0.30, 0.36, 0.42), smoothstep(0.4, 1.0, f.y));
            float awn = step(0.7, hash12(cell * 0.37 + 4.0));
            col = mix(shade, g2, win);
            col = mix(col, mix(vec3(0.62, 0.70, 0.74), base, 0.4), rail * 0.85);
            col = mix(col, base * 1.04, clamp(slab + pier, 0.0, 1.0));
            col = mix(col, vec3(0.78, 0.74, 0.62), awn * box(f, vec2(0.1, 0.85), vec2(0.9, 1.0), aa) * 0.8);
            if (gnd > 0.5) col = mix(base * 0.8, glass * 1.3, box(f, vec2(0.08, 0.08), vec2(0.92, 0.85), aa));
            material.roughness = mix(0.85, 0.35, win * (1.0 - rail));
          }
          col = mix(classic ? mix(base, glass, 0.18) : (kind > 9.5 ? mix(base, vec3(0.16, 0.22, 0.28), 0.45) : mix(base, base * 0.6, 0.35)), col, detail);
          // Soft darkening at the foot of walls (ambient occlusion).
          col *= mix(0.72, 1.0, smoothstep(0.0, 1.2, uv.y));
          material.diffuse = col;
        } else if (kind < 3.5) {        // flat roofs
          material.diffuse = base * (0.95 + 0.08 * hash12(floor(fsInput.attributes.positionMC.xz * 0.5)));
        } else if (kind < 4.5) {        // tiled roofs: rows of tiles
          float rows = fract(fsInput.attributes.positionMC.y * 3.0);
          material.diffuse = base * mix(0.86, 1.04, smoothstep(0.0, 0.25, rows));
          material.roughness = 0.75;
        } else if (kind < 5.5) {        // leaves
          vec3 n = fsInput.attributes.normalEC;
          material.diffuse = base * (0.9 + 0.2 * hash12(floor(fsInput.attributes.positionMC.xz * 2.0)));
          material.roughness = 0.9;
        } else if (kind < 7.5) {        // wood, boats
          vec2 f = fract(uv);
          float band = kind > 6.5 ? smoothstep(0.42, 0.46, f.y) * (1.0 - smoothstep(0.7, 0.74, f.y)) * step(0.5, uv.y + 0.6) * step(0.05, uv.y) : 0.0;
          material.diffuse = mix(base, vec3(0.04, 0.06, 0.08), band * 0.9);
          material.roughness = kind > 6.5 ? 0.35 : 0.9;
          material.specular = vec3(kind > 6.5 ? 0.06 : 0.02);
        } else if (kind < 8.5) {        // sea: moving ripples and sun glints
          vec3 p = fsInput.attributes.positionMC;
          float t = u_time;
          vec2 q = p.xz;
          // A few crossing swells plus fine chop, small slopes only.
          vec2 g = vec2(0.0);
          g += vec2(0.11, 0.07) * cos(dot(q, vec2(0.11, 0.07)) + t * 1.1);
          g += vec2(-0.05, 0.13) * cos(dot(q, vec2(-0.05, 0.13)) + t * 0.8);
          g += vec2(0.29, -0.21) * 0.5 * cos(dot(q, vec2(0.29, -0.21)) + t * 2.1);
          g += vec2(-0.37, -0.41) * 0.35 * cos(dot(q, vec2(-0.37, -0.41)) + t * 2.7);
          g += vec2(0.61, 0.17) * 0.2 * cos(dot(q, vec2(0.61, 0.17)) + t * 3.3);
          float near = 1.0 - clamp(length(fsInput.attributes.positionEC) / 1500.0, 0.0, 0.85);
          vec3 nMC = normalize(vec3(0.12 * g.x * near, 1.0, 0.12 * g.y * near));
          material.normalEC = normalize(czm_normal * nMC);
          float far = clamp(length(fsInput.attributes.positionEC) / 3000.0, 0.0, 1.0);
          material.diffuse = mix(base, base * vec3(0.8, 0.95, 1.05), far) * 0.55;
          material.roughness = 0.08;
          material.specular = vec3(0.04);
        } else {
          material.diffuse = base;
        }
      }
    `,
  });

  let model = null, loading = null, active = false, meta = null;
  let clip = null;
  const saved = {};

  function frame() {
    const o = Cesium.Cartesian3.fromDegrees(meta.origin.lon, meta.origin.lat, meta.origin.height);
    return Cesium.Transforms.eastNorthUpToFixedFrame(o);
  }

  async function load() {
    if (loading) return loading;
    loading = (async () => {
      meta = await Cesium.Resource.fetchJson({ url: `${URL_BASE}.json` });
      model = await Cesium.Model.fromGltfAsync({
        url: `${URL_BASE}.glb`,
        modelMatrix: frame(),
        // The file is written y-up with x = east, z = south; no extra turn.
        upAxis: Cesium.Axis.Y,
        forwardAxis: Cesium.Axis.X,
        customShader: shader,
        shadows: Cesium.ShadowMode.ENABLED,
        backFaceCulling: true,
        // Stay sharp: everything is in one file anyway.
        maximumScreenSpaceError: 2,
      });
      model.show = active;
      scene.primitives.add(model);
      // The open-data globe stays around the set, but not inside it.
      const ring = meta.clip.map(([lon, lat]) => Cesium.Cartesian3.fromDegrees(lon, lat));
      if (Cesium.ClippingPolygonCollection && Cesium.ClippingPolygonCollection.isSupported(scene)) {
        clip = new Cesium.ClippingPolygonCollection({
          polygons: [new Cesium.ClippingPolygon({ positions: ring })],
          enabled: false,
        });
        scene.globe.clippingPolygons = clip;
      }
      await new Promise((resolve) => {
        if (model.ready) return resolve();
        const rm = model.readyEvent.addEventListener(() => { rm(); resolve(); });
      });
      return model;
    })();
    return loading;
  }

  // The sea ripples.
  const t0 = performance.now();
  scene.preRender.addEventListener(() => {
    if (active) shader.setUniform("u_time", (performance.now() - t0) / 1000);
  });

  // Shadows sell the film look. On phones they start on and switch off by
  // themselves if the frame rate drops below about 30 fps.
  let fpsWatch = null;
  function watchFps() {
    if (!MOBILE || fpsWatch) return;
    let frames = 0, since = performance.now(), strikes = 0;
    fpsWatch = scene.postRender.addEventListener(() => {
      frames++;
      const now = performance.now();
      if (now - since < 2000) return;
      const fps = (frames * 1000) / (now - since);
      frames = 0; since = now;
      if (active && viewer.shadows && fps < 28) {
        if (++strikes >= 2) { viewer.shadows = false; }
      } else strikes = 0;
    });
  }

  const loadingEl = document.getElementById("loading");
  async function setFilm(on) {
    if (on === active) return;
    active = on;
    document.body.classList.toggle("filmset", on);
    btn.classList.toggle("on", on);
    if (on) {
      document.querySelectorAll("[data-mode]").forEach((o) => o.classList.remove("on"));
      const real = document.getElementById("photoreal");
      if (real) real.classList.remove("on");
      if (M.setPhotorealOff) await M.setPhotorealOff();
      M.hideOpenData(true);
      saved.shadows = viewer.shadows;
      saved.time = viewer.clock.currentTime;
      saved.minZoom = scene.screenSpaceCameraController.minimumZoomDistance;
      saved.fog = scene.fog.density;
      saved.depth = scene.globe.depthTestAgainstTerrain;
      // The sea plane spreads past the set; hills around it must hide it.
      scene.globe.depthTestAgainstTerrain = true;
      // Early-morning sun from the sea (08:15 in June): long soft shadows
      // and warm light on the façades that face the harbour.
      viewer.clock.currentTime = Cesium.JulianDate.fromIso8601("2026-06-21T06:15:00Z");
      saved.light = scene.light;
      scene.light = new Cesium.SunLight({ color: new Cesium.Color(1.0, 0.93, 0.82), intensity: 2.6 });
      viewer.shadows = true;
      viewer.shadowMap.size = MOBILE ? 2048 : 4096;
      viewer.shadowMap.softShadows = !MOBILE;
      viewer.shadowMap.maximumDistance = MOBILE ? 1200 : 2500;
      viewer.shadowMap.darkness = 0.45;
      scene.screenSpaceCameraController.minimumZoomDistance = 12;
      scene.fog.density = 0.0004;
      loadingEl.lastChild.textContent = "Building the film set…";
      loadingEl.hidden = false;
      try {
        await load();
      } catch (e) {
        console.error("film set failed", e);
        loadingEl.hidden = true;
        btn.textContent = "Film set unavailable";
        btn.disabled = true;
        active = false;
        setFilm(false);
        return;
      }
      loadingEl.hidden = true;
      loadingEl.lastChild.textContent = "Loading Monaco…";
      if (!active) return;
      model.show = true;
      if (clip) clip.enabled = true;
      watchFps();
    } else {
      if (model) model.show = false;
      if (clip) clip.enabled = false;
      if (saved.time) {
        viewer.clock.currentTime = saved.time;
        scene.light = saved.light;
        viewer.shadows = saved.shadows;
        scene.screenSpaceCameraController.minimumZoomDistance = saved.minZoom;
        scene.fog.density = saved.fog;
        scene.globe.depthTestAgainstTerrain = saved.depth;
      }
      M.hideOpenData(false);
    }
  }

  // "Film set" sits next to Photoreal / Surface / Points.
  const row = document.querySelector("[data-mode=surface]").parentElement;
  const btn = document.createElement("button");
  btn.id = "filmset";
  btn.textContent = "Film set";
  btn.title = "Our own model of Monte-Carlo, built from open data";
  row.insertBefore(btn, row.firstChild);
  btn.onclick = async () => {
    await setFilm(true);
    M.view(M.PLACES.find((p) => p.name === "Monte-Carlo"), 3);
  };
  // Leaving: any other view button turns the film set off first.
  row.querySelectorAll("button").forEach((b) => {
    if (b !== btn) b.addEventListener("click", () => setFilm(false), true);
  });

  M.filmset = { setFilm, isOn: () => active, getModel: () => model };

  if (new URLSearchParams(location.search).get("mode") === "film") {
    setFilm(true).then(() => {
      if (!new URLSearchParams(location.search).get("place")) {
        M.view(M.PLACES.find((p) => p.name === "Monte-Carlo"), 0);
      }
    });
  }
})();
