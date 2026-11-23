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

  // Kinds written by the pipeline into the colour alpha: kind * 16 + style.
  // 0 ground, 1 classic wall, 2 modern wall, 3 flat roof, 4 tiled roof,
  // 5 leaves, 6 wood, 7 boat, 8 sea, 9 plain, 10 glass wall, 11 road
  // surface (style = lane markings, texCoord_1 = across/along). For walls the
  // style is the façade type (pipeline S_*): 0 Riviera shutters, 1 Belle
  // Époque, 2 arched (Casino), 3 painted villa, 4 1960s-80s loggias,
  // 5 1970s balcony bands, 6 glass, 7 contemporary, 8 1930s.
  const shader = new Cesium.CustomShader({
    lightingModel: Cesium.LightingModel.PBR,
    uniforms: {
      u_time: { type: Cesium.UniformType.FLOAT, value: 0 },
      // 1 while the Legendary laps run: the Grand Prix crowd is there only
      // for the race.
      u_crowd: { type: Cesium.UniformType.FLOAT, value: 0 },
      // 0 day, 1 night: lit windows, street lamps and their pools of light.
      u_night: { type: Cesium.UniformType.FLOAT, value: 0 },
      // Cesium flips a uniform texture's rows on upload (a glTF's own
      // texture is not), so it is read at (u, 1 - v) below.
      u_lights: { type: Cesium.UniformType.SAMPLER_2D, value: new Cesium.TextureUniform({ url: `${URL_BASE}-night.jpg` }) },
    },
    // Spectators (plain, style 4): uv.y = 1 marks the parts that jump and
    // wave, uv.x the phase, so the crowd bounces out of step.
    vertexShaderText: `
      void vertexMain(VertexInput vsInput, inout czm_modelVertexOutput vsOutput) {
        float a255 = floor(vsInput.attributes.color_0.a * 255.0 + 0.5);
        if (abs(a255 - 148.0) < 0.5) {
          if (u_crowd < 0.5) { vsOutput.positionMC = vec3(0.0); return; }   // folded away
          vec2 uv = vsInput.attributes.texCoord_0;
          float ph = uv.x * 6.2832;
          float hop = max(0.0, sin(u_time * 6.0 + ph * 3.0)) * 0.12 * (0.5 + 0.5 * sin(ph * 7.0));
          // positionMC is in the mesh's quantised units: metres / node scale.
          float sc = length(czm_model[0].xyz);
          vsOutput.positionMC.y += uv.y * hop / max(sc, 1e-6);
        }
      }
    `,
    fragmentShaderText: `
      float hash12(vec2 p) {
        vec3 p3 = fract(vec3(p.xyx) * 0.1031);
        p3 += dot(p3, p3.yzx + 33.33);
        return fract((p3.x + p3.y) * p3.z);
      }
      float vnoise(vec2 p) {
        vec2 i = floor(p), f = fract(p);
        f = f * f * (3.0 - 2.0 * f);
        return mix(mix(hash12(i), hash12(i + vec2(1.0, 0.0)), f.x),
                   mix(hash12(i + vec2(0.0, 1.0)), hash12(i + vec2(1.0, 1.0)), f.x), f.y);
      }
      float box(vec2 f, vec2 lo, vec2 hi, vec2 aa) {
        vec2 a = smoothstep(lo - aa, lo + aa, f) * (1.0 - smoothstep(hi - aa, hi + aa, f));
        return a.x * a.y;
      }
      // Round-headed opening: straight jambs from y0 to ys, a half circle above.
      float archMask(vec2 f, float x0, float x1, float y0, float ys, vec2 aa) {
        float rr = (x1 - x0) * 0.5;
        float rect = box(f, vec2(x0, y0), vec2(x1, ys), aa);
        float d = length((f - vec2((x0 + x1) * 0.5, ys)) / vec2(1.0, 1.0));
        float top = (1.0 - smoothstep(rr - aa.x, rr + aa.x, d)) * step(ys, f.y);
        return max(rect, top);
      }
      // Roman (canal) tiles: a: across the slope, s: down it, in metres;
      // aa: their screen footprints. Rounded caps in rows across the slope
      // with dark channels between them, each course overlapping the next
      // with a shadowed lip, every tile a slightly different fired colour.
      vec3 romanTiles(float a, float s, float aaA, float aaS) {
        float fa = fract(a / 0.42), fs = fract(s / 0.40);
        float capW = 1.0 - smoothstep(0.3, 0.6, aaA / 0.42);       // fades once a row is under ~3 px
        float lipW = 1.0 - smoothstep(0.3, 0.6, aaS / 0.40);
        float cap = sin(fa * 3.1416);                         // 0 in the channel, 1 on the crown
        float shade = mix(1.0, 0.72 + 0.4 * pow(cap, 0.7), capW);
        float lip = smoothstep(0.78, 0.97, fs) * 0.4 * lipW;            // shadow under the next course
        float lit = (1.0 - smoothstep(0.0, 0.12, fs)) * 0.12 * lipW;      // the course's sunlit front edge
        float w = capW * lipW;
        float h = hash12(floor(vec2(a / 0.42, s / 0.40)) + 11.0) - 0.5;
        vec3 tone = mix(vec3(1.0), vec3(1.0 + 0.16 * h, 1.0 + 0.05 * h, 1.0 - 0.10 * h) * (0.93 + 0.14 * hash12(floor(vec2(a / 0.42, s / 0.40)))), w);
        return tone * shade * (1.0 - lip + lit);
      }
      void fragmentMain(FragmentInput fsInput, inout czm_modelMaterial material) {
        vec4 c = fsInput.attributes.color_0;
        float a255 = floor(c.a * 255.0 + 0.5);
        float kind = floor(a255 / 16.0);
        if (kind < 0.5) {               // ground: the painted map
          material.roughness = 0.95;
          vec3 mapC = material.diffuse;
          {
            // Close up, the 37 cm map gets fine detail of its own: grain
            // and patches everywhere, paving joints on the pale stone of
            // pavements and squares. It fades out with distance.
            vec2 gp = fsInput.attributes.positionMC.xz;
            float px = length(fwidth(gp));
            float near = 1.0 - smoothstep(0.08, 0.5, px);
            if (near > 0.0) {
              vec3 d = material.diffuse;
              float grain = mix(0.94, 1.05, vnoise(gp * 1.7)) * mix(0.97, 1.03, vnoise(gp * 9.0));
              float lum = dot(d, vec3(0.3, 0.59, 0.11));
              float sat = max(max(d.r, d.g), d.b) - min(min(d.r, d.g), d.b);
              float paleStone = smoothstep(0.30, 0.45, lum) * (1.0 - smoothstep(0.10, 0.22, sat));
              vec2 pv = fract(gp / vec2(0.6, 0.4) + vec2(0.5 * step(0.5, fract(gp.y / 0.8)), 0.0));
              vec2 pw = fwidth(gp / vec2(0.6, 0.4)) * 1.2;
              float joint = 1.0 - box(pv, vec2(0.04), vec2(0.96), pw);
              d *= grain;
              d *= 1.0 - 0.12 * joint * paleStone * (1.0 - smoothstep(0.03, 0.08, px));
              // Lawns: clumps, darker tufts and fine blades instead of a
              // flat green wash; a little lighter in mown stripes.
              float grass = smoothstep(0.02, 0.07, d.g - max(d.r, d.b));
              if (grass > 0.0) {
                float clump = vnoise(gp * 2.3) * 0.6 + vnoise(gp * 6.1) * 0.4;
                float blade = hash12(floor(gp * 28.0));
                float bladeFade = 1.0 - smoothstep(0.015, 0.05, px);
                float stripe = step(0.5, fract(dot(gp, vec2(0.71, 0.71)) / 3.2));
                vec3 g = d * mix(0.78, 1.12, clump);
                g = mix(g, g * vec3(1.08, 1.06, 0.82), smoothstep(0.62, 0.8, vnoise(gp * 0.9)) * 0.6);
                g *= mix(1.0, mix(0.86, 1.1, blade), bladeFade);
                g *= 1.0 + 0.05 * stripe;
                d = mix(d, g, grass);
              }
              material.diffuse = mix(material.diffuse, d, near);
            }
            {
              // Further out (to ~60 m from the car), keep some grass texture
              // so lawns do not turn into a flat smear.
              vec3 d = material.diffuse;
              float grass = smoothstep(0.02, 0.07, d.g - max(d.r, d.b));
              float mid = smoothstep(0.3, 0.5, px) * (1.0 - smoothstep(1.5, 3.0, px));
              if (grass * mid > 0.0) {
                float clump = vnoise(gp * 0.8) * 0.6 + vnoise(gp * 2.3) * 0.4;
                material.diffuse = mix(d, d * mix(0.82, 1.1, clump), grass * mid);
              }
            }
          }
          {
            // Cliffs: steep ground is bare limestone, pale and layered,
            // with the plants that cling to it, not the aerial photo
            // smeared down a wall.
            vec3 upEC = normalize(czm_normal * vec3(0.0, 1.0, 0.0));
            float slope = dot(normalize(fsInput.attributes.normalEC), upEC);
            float steep = 1.0 - smoothstep(0.5, 0.74, slope);
            if (steep > 0.0) {
              vec3 p = fsInput.attributes.positionMC;
              vec3 d = material.diffuse;
              float veg = smoothstep(0.01, 0.05, d.g - max(d.r, d.b)) * smoothstep(0.35, 0.65, vnoise(p.xz * 0.21 + p.y * 0.13));
              float strata = vnoise(vec2(p.y * 1.1 + vnoise(p.xz * 0.12) * 2.5, 0.37));
              float blotch = vnoise(p.xz * 0.33 + vec2(p.y * 0.27));
              float crack = smoothstep(0.78, 0.9, vnoise(vec2(dot(p.xz, vec2(0.7, 0.7)) * 1.9, p.y * 0.6)));
              vec3 lime = czm_srgbToLinear(vec3(0.80, 0.75, 0.65));
              vec3 rock = lime * mix(0.78, 1.06, blotch) * mix(0.86, 1.04, strata) * (1.0 - 0.35 * crack);
              rock = mix(rock, rock * vec3(1.06, 0.95, 0.80), smoothstep(0.55, 0.8, vnoise(p.xz * 0.09 + p.y * 0.05)) * 0.6);
              material.diffuse = mix(d, rock, steep * (1.0 - 0.7 * veg));
            }
          }
          if (a255 > 8.5) {
            // Pavements along the circuit (style 9 red brick pavers, 10
            // pale stone slabs): laid brick by brick in world metres, so
            // they stay crisp up close instead of the 37 cm map's blur.
            // The map still gives the shade (sun shadows, darker at the
            // foot of walls): its brightness against the paving colour.
            float sc = length(czm_model[0].xyz);
            vec2 wp = fsInput.attributes.positionMC.xz * sc;
            float px = length(fwidth(wp));
            bool brick = a255 < 9.5;
            vec3 ref = czm_srgbToLinear(brick ? vec3(0.647, 0.443, 0.373) : vec3(0.827, 0.796, 0.741));
            float shade = clamp(dot(mapC, vec3(0.3, 0.59, 0.11)) / dot(ref, vec3(0.3, 0.59, 0.11)), 0.4, 1.1);
            vec2 bs = brick ? vec2(0.20, 0.10) : vec2(0.60, 0.40);
            vec2 q = wp / bs;
            q.x += 0.5 * step(0.5, fract(q.y * 0.5));
            vec2 cell = floor(q), f = fract(q);
            vec2 aa = max(fwidth(q) * 1.2, vec2(0.001));
            vec2 jw = brick ? vec2(0.04, 0.08) : vec2(0.012, 0.018);
            float face = box(f, jw, 1.0 - jw, aa);
            float h = hash12(cell);
            vec3 col = ref * (brick ? mix(0.8, 1.14, h) : mix(0.92, 1.06, h));
            if (brick) col = mix(col, ref * vec3(0.78, 0.86, 0.95), step(0.92, hash12(cell + 7.3)) * 0.6);
            else col *= 1.0 - 0.06 * vnoise(wp * 3.1 + h * 9.0);
            vec3 joint = brick ? ref * vec3(0.62, 0.6, 0.6) : ref * 0.72;
            col = mix(joint, col, face);
            // Far away the pattern turns into its own average.
            float near = 1.0 - smoothstep(brick ? 0.025 : 0.06, brick ? 0.07 : 0.2, px);
            col = mix(ref * (brick ? 0.93 : 0.97), col, near);
            // Wear and grime.
            col *= mix(0.9, 1.05, vnoise(wp * 0.45)) * mix(0.96, 1.03, vnoise(wp * 2.2));
            material.diffuse = col * shade;
            material.roughness = 0.9;
          }
          if (u_night > 0.0) {
            float L = texture(u_lights, vec2(fsInput.attributes.texCoord_0.x, 1.0 - fsInput.attributes.texCoord_0.y)).r;
            material.emissive = material.diffuse * vec3(1.0, 0.78, 0.5) * L * 2.4 * u_night;
          }
          return;
        }
        if (kind > 10.5) {              // road surface: asphalt and markings
          // texCoord_1: across (+16) / 32 and along / 72, in metres.
          vec2 r2 = fsInput.attributes.texCoord_1;
          float ac = r2.x * 32.0 - 16.0;
          float al = r2.y * 72.0;
          float hw = c.b * 25.5;                 // half width, m
          float junc = c.r;                      // 0 at junctions: no markings
          float zebra = c.g;
          float mk = a255 - kind * 16.0;
          // World position in metres (the ground map's coordinates), so
          // overlapping strips at junctions show the same asphalt.
          vec2 wp = fsInput.attributes.texCoord_0 * 1500.0;
          float px = length(fwidth(wp));
          // Asphalt: dark grey aggregate, sun-bleached and patched.
          vec3 asph = vec3(0.115, 0.118, 0.125);
          float patchN = vnoise(wp * 0.11) * 0.6 + vnoise(wp * 0.37) * 0.4;
          asph *= mix(0.86, 1.18, patchN);
          // Repairs: darker rectangles of newer tarmac here and there.
          vec2 cellR = floor(wp / vec2(7.0, 4.0));
          float rep = step(0.86, hash12(cellR)) * box(fract(wp / vec2(7.0, 4.0)), vec2(0.15), vec2(0.85), vec2(0.03));
          asph *= 1.0 - 0.22 * rep;
          // Aggregate speckle up close.
          float near = 1.0 - smoothstep(0.04, 0.25, px);
          asph *= mix(1.0, mix(0.82, 1.16, hash12(floor(wp * 22.0))), near);
          // Tyre tracks: polished, slightly darker bands in each lane;
          // dust and grit lighter along the kerbs.
          float lanes = max(1.0, floor(2.0 * hw / 3.0 + 0.3));
          float lw = 2.0 * hw / lanes;
          float inl = fract((ac + hw) / lw) * lw;            // 0..lw across the lane
          float tyre = smoothstep(0.35, 0.0, abs(inl - lw * 0.3)) + smoothstep(0.35, 0.0, abs(inl - lw * 0.7));
          asph *= 1.0 - 0.07 * tyre * junc;
          asph *= 1.0 + 0.10 * (1.0 - smoothstep(0.0, 0.5, hw - abs(ac)));
          // Fine cracks.
          float crack = smoothstep(0.93, 0.98, vnoise(wp * 1.3 + vnoise(wp * 0.4) * 3.0));
          asph *= 1.0 - 0.25 * crack * near;
          // Markings (white paint, worn).
          vec2 aa = vec2(max(fwidth(ac), 0.01) * 1.2, max(fwidth(al), 0.01) * 1.2);
          float paint = 0.0;
          float dash = smoothstep(0.0, aa.y, fract(al / 9.0) * 9.0) * (1.0 - smoothstep(3.0 - aa.y, 3.0 + aa.y, fract(al / 9.0) * 9.0));
          if (mk > 0.5 && mk < 2.5) {           // centre line, dashed
            paint = max(paint, (1.0 - smoothstep(0.06, 0.06 + aa.x, abs(ac))) * dash);
          }
          if (mk > 1.5) {                       // edge lines
            float e = abs(abs(ac) - (hw - 0.35));
            paint = max(paint, 1.0 - smoothstep(0.06, 0.06 + aa.x, e));
          }
          if (mk > 2.5 && mk < 3.5) {           // lane lines, dashed
            float d = abs(fract((ac + hw) / lw + 0.5) - 0.5) * lw;
            float inner = step(0.5 * lw, hw - abs(ac));
            paint = max(paint, (1.0 - smoothstep(0.06, 0.06 + aa.x, d)) * dash * inner);
          }
          paint *= junc;
          if (zebra > 0.5) {                    // zebra crossing: bars along the road
            float bars = box(vec2(fract((ac + hw) / 1.0), 0.5), vec2(0.0, 0.0), vec2(0.5, 1.0), vec2(aa.x, 0.01));
            paint = max(paint, bars * step(abs(ac), hw - 0.3));
          }
          paint *= mix(0.7, 1.0, vnoise(wp * 2.7)) * mix(0.85, 1.0, hash12(floor(wp * 12.0)));
          vec3 white = vec3(0.78, 0.78, 0.75);
          material.diffuse = mix(asph, white, clamp(paint, 0.0, 1.0));
          material.roughness = mix(0.92, 0.6, paint);
          material.specular = vec3(0.03);
          if (u_night > 0.0) {
            float L = texture(u_lights, vec2(fsInput.attributes.texCoord_0.x, 1.0 - fsInput.attributes.texCoord_0.y)).r;
            material.emissive = material.diffuse * vec3(1.0, 0.78, 0.5) * L * 2.4 * u_night;
          }
          return;
        }
        vec3 base = czm_srgbToLinear(c.rgb);
        vec2 uv = fsInput.attributes.texCoord_0;
        material.roughness = 0.85;
        material.specular = vec3(0.03);
        if (kind < 2.5 || kind > 9.5) {  // walls: one of nine real façade types
          float style = a255 - kind * 16.0;
          vec2 f = fract(uv);
          vec2 cell = floor(uv);
          vec2 aa = max(fwidth(uv) * 1.2, vec2(0.002));
          // Fade the pattern out far away instead of letting it shimmer.
          float detail = 1.0 - smoothstep(0.18, 0.45, max(aa.x, aa.y));
          float fine = 1.0 - smoothstep(0.025, 0.07, max(aa.x, aa.y));   // louvres, bars, stripes
          float r = hash12(cell + 17.0);
          float vr = hash12(floor(c.rgb * 255.0 + 0.5).rg + floor(c.b * 255.0 + 0.5) * 0.03);   // per building
          vec3 col = base;
          vec3 glass = mix(vec3(0.05, 0.07, 0.09), vec3(0.22, 0.28, 0.34), smoothstep(0.3, 1.0, f.y));
          bool lowF = uv.y < 1.0;          // the ground floor
          // texCoord_1.x: 1 where a real shop (OpenStreetMap) is behind
          // the wall, 0 for flats, hotel and office lobbies.
          bool shopF = fsInput.attributes.texCoord_1.x > 0.5;
          bool gnd = lowF && shopF;        // a shopfront
          float shine = 0.0;               // how much of this pixel is glass
          float shopGlow = 0.0;            // lit boutique window (Casino, Café de Paris)
          vec3 farCol = mix(base, glass, 0.18);
          if (uv.y < 0.0) {
            col = base * 0.7;              // plinth below street level
          } else if (style < 0.5 || (style > 2.5 && style < 3.5)) {
            // Riviera house (0) and painted villa (3): tall windows with
            // louvred shutters, sills; villas have white surrounds.
            bool villa = style > 2.5;
            vec3 surround = villa ? vec3(0.93, 0.90, 0.84) : base * 1.12;
            float sill = box(f, vec2(0.28, 0.14), vec2(0.72, 0.19), aa);
            if (gnd && !villa) {
              float shop = box(f, vec2(0.1, 0.06), vec2(0.9, 0.78), aa);
              float awning = box(f, vec2(0.06, 0.78), vec2(0.94, 0.9), aa);
              vec3 aw = r < 0.33 ? vec3(0.55, 0.12, 0.10) : (r < 0.66 ? vec3(0.13, 0.25, 0.20) : vec3(0.75, 0.70, 0.60));
              col = mix(col, glass * 1.5 + vec3(0.04, 0.03, 0.02), shop);
              col = mix(col, vec3(0.15), box(f, vec2(0.1, 0.06), vec2(0.9, 0.1), aa) * 0.6);
              col = mix(col, aw, awning * step(0.25, r));
              shine = shop;
            } else {
              vec2 lo = vec2(0.36, 0.2), hi = vec2(0.64, 0.84);
              float win = box(f, lo, hi, aa);
              float frame = box(f, lo - (villa ? 0.07 : 0.035), hi + (villa ? 0.07 : 0.035), aa);
              float shut = box(f, vec2(0.18, 0.2), vec2(0.355, 0.84), aa) + box(f, vec2(0.645, 0.2), vec2(0.82, 0.84), aa);
              vec3 sc = vr < 0.5 ? vec3(0.22, 0.33, 0.24) : (vr < 0.75 ? vec3(0.42, 0.52, 0.52) : vec3(0.55, 0.42, 0.25));
              sc *= 0.85 + 0.3 * hash12(cell * 1.7);
              float louv = mix(0.94, 0.88 + 0.12 * step(0.5, fract(f.y * 22.0)), fine);
              bool closed = r < 0.2;
              bool half_ = r > 0.2 && r < 0.3;
              col = mix(col, surround, frame);
              col = mix(col, base * 1.15, sill);
              col = mix(col, glass, win);
              // Recess: the lintel and one side of the opening in shadow.
              col *= 1.0 - 0.45 * win * box(f, vec2(lo.x, hi.y - 0.07), hi, aa);
              col *= 1.0 - 0.3 * win * box(f, lo, vec2(lo.x + 0.04, hi.y), aa);
              col = mix(col, vec3(0.85, 0.82, 0.74), win * box(f, vec2(0.495, lo.y), vec2(0.505, hi.y), aa));
              if (closed) col = mix(col, sc * louv, win);
              else if (half_) col = mix(col, sc * louv, win * step(0.5, f.y));
              col = mix(col, sc * louv, shut * (closed ? 0.0 : 1.0));
              // Wrought-iron balconette on some floors.
              float rail = box(f, vec2(0.3, 0.2), vec2(0.7, 0.36), aa) * step(0.55, hash12(vec2(cell.y, 3.0) + vr));
              float bars = step(0.6, fract(f.x * 30.0)) + box(f, vec2(0.3, 0.33), vec2(0.7, 0.36), aa);
              col = mix(col, vec3(0.1), rail * mix(0.35, clamp(bars, 0.0, 1.0), fine) * 0.85);
              shine = win * (closed ? 0.0 : 1.0);
            }
          } else if (style < 1.5) {
            // Belle Époque palace: rusticated, arched ground floor; tall
            // French windows with moulded surrounds and hoods; a stone
            // balcony along the first floor, iron balconettes above.
            if (lowF) {
              float groove = 1.0 - 0.18 * (1.0 - smoothstep(0.0, 0.05, abs(fract(f.y * 4.0) - 0.5) * 2.0 - 0.9));
              col = base * groove;
              float arch_ = archMask(f, 0.18, 0.82, 0.0, 0.58, aa);
              float archF = archMask(f, 0.14, 0.86, 0.0, 0.58, aa);
              col = mix(col, base * 1.1, archF);
              col = mix(col, glass * 1.6 + vec3(0.06, 0.04, 0.0), arch_);
              col = mix(col, vec3(0.12), arch_ * box(f, vec2(0.18, 0.5), vec2(0.82, 0.53), aa));
              col = mix(col, base * 1.2, box(f, vec2(0.45, 0.85), vec2(0.55, 0.96), aa));   // keystone
              shine = arch_;
            } else {
              vec2 lo = vec2(0.31, 0.1), hi = vec2(0.69, 0.8);
              float win = box(f, lo, hi, aa);
              float frame = box(f, lo - vec2(0.05, 0.03), hi + vec2(0.05, 0.03), aa);
              float hood = box(f, vec2(0.24, 0.86), vec2(0.76, 0.93), aa);
              float hoodSh = box(f, vec2(0.24, 0.83), vec2(0.76, 0.86), aa);
              float band = box(f, vec2(-1.0, 0.0), vec2(2.0, 0.035), aa);
              col = mix(col, base * 1.14, frame);
              col = mix(col, base * 0.82, hoodSh);
              col = mix(col, base * 1.2, hood);
              col = mix(col, base * 1.1, band);
              vec3 g = glass;
              if (vr < 0.35) {             // some palaces keep their shutters
                float shut = box(f, vec2(0.15, 0.1), vec2(0.255, 0.8), aa) + box(f, vec2(0.745, 0.1), vec2(0.85, 0.8), aa);
                col = mix(col, vec3(0.62, 0.64, 0.58) * mix(0.95, 0.9 + 0.1 * step(0.5, fract(f.y * 22.0)), fine), shut);
              }
              if (r < 0.25) g = mix(g, vec3(0.80, 0.76, 0.66), step(0.35, f.y) * 0.8);   // drawn curtains
              col = mix(col, g, win);
              col *= 1.0 - 0.45 * win * box(f, vec2(lo.x, hi.y - 0.06), hi, aa);
              col *= 1.0 - 0.3 * win * box(f, lo, vec2(lo.x + 0.04, hi.y), aa);
              col = mix(col, base * 1.1, win * (box(f, vec2(0.495, lo.y), vec2(0.505, hi.y), aa) + box(f, vec2(lo.x, 0.6), vec2(hi.x, 0.62), aa)));
              bool piano = cell.y < 1.5 || (vr > 0.6 && abs(cell.y - 4.0) < 0.5);
              if (piano) {               // continuous balcony
                float slab = box(f, vec2(-1.0, 0.0), vec2(2.0, 0.06), aa);
                float rail = box(f, vec2(-1.0, 0.06), vec2(2.0, 0.3), aa);
                float bal = step(0.55, fract(f.x * 9.0)) + box(f, vec2(-1.0, 0.27), vec2(2.0, 0.3), aa);
                col = mix(col, base * 1.18, rail * mix(0.6, clamp(bal, 0.0, 1.0), fine));
                col = mix(col, base * 0.75, slab * 0.4 + rail * (1.0 - clamp(bal, 0.0, 1.0)) * 0.0);
                col = mix(col, base * 1.15, slab);
              } else {
                float rail = box(f, vec2(0.27, 0.1), vec2(0.73, 0.32), aa);
                float bars = step(0.62, fract(f.x * 34.0)) + box(f, vec2(0.27, 0.29), vec2(0.73, 0.32), aa) + box(f, vec2(0.27, 0.1), vec2(0.73, 0.13), aa);
                col = mix(col, vec3(0.09, 0.09, 0.08), rail * mix(0.35, clamp(bars, 0.0, 1.0), fine) * 0.9);
              }
              shine = win;
            }
          } else if (style < 2.5 && gnd) {
            // Casino / Café de Paris boutiques (a real shop behind the bay):
            // the whole arch glazed in a bronze surround, a dark fascia with
            // a gilt line at transom height, the shop lit inside.
            float pil = 1.0 - box(f, vec2(0.07, -1.0), vec2(0.93, 2.0), aa);
            float arch_ = archMask(f, 0.14, 0.86, 0.03, 0.62, aa);
            float surround = archMask(f, 0.1, 0.9, 0.0, 0.62, aa) * (1.0 - arch_);
            float fascia = box(f, vec2(0.14, 0.47), vec2(0.86, 0.54), aa);
            float gilt = box(f, vec2(0.3, 0.498), vec2(0.7, 0.512), aa);
            float mull = box(f, vec2(0.494, 0.03), vec2(0.506, 0.47), aa) + box(f, vec2(0.14, 0.03), vec2(0.86, 0.045), aa);
            col = mix(base * 1.03, base * 1.13, pil);
            col = mix(col, base * 0.85, pil * box(f, vec2(0.07, -1.0), vec2(0.09, 2.0), aa));
            col = mix(col, vec3(0.2, 0.15, 0.09), surround);
            col = mix(col, vec3(0.05, 0.055, 0.06), arch_);
            col = mix(col, vec3(0.07, 0.065, 0.06), arch_ * clamp(fascia + mull, 0.0, 1.0));
            col = mix(col, vec3(0.78, 0.6, 0.3), arch_ * gilt);
            col = mix(col, base * 1.2, box(f, vec2(0.46, 0.86), vec2(0.54, 0.97), aa));     // keystone
            shopGlow = arch_ * (1.0 - clamp(fascia + mull, 0.0, 1.0)) * mix(1.0, 0.45, step(0.54, f.y));
            shine = arch_;
          } else if (style < 2.5) {
            // Casino / Café de Paris: round-arched windows between pilasters.
            float pil = 1.0 - box(f, vec2(0.08, -1.0), vec2(0.92, 2.0), aa);
            float archF = archMask(f, 0.2, 0.8, 0.08, 0.6, aa);
            float arch_ = archMask(f, 0.25, 0.75, 0.1, 0.6, aa);
            col = mix(col, base * 1.08, archF);
            col = mix(col, base * 1.12, pil);
            col = mix(col, base * 0.85, pil * box(f, vec2(0.08, -1.0), vec2(0.1, 2.0), aa));
            vec3 g = lowF ? glass * 1.5 + vec3(0.07, 0.05, 0.0) : glass;
            col = mix(col, g, arch_);
            col = mix(col, base * 1.1, arch_ * (box(f, vec2(0.49, 0.1), vec2(0.51, 0.9), aa) + box(f, vec2(0.25, 0.58), vec2(0.75, 0.6), aa)));
            col *= 1.0 - 0.35 * arch_ * box(f, vec2(0.25, 0.1), vec2(0.29, 0.62), aa);
            col = mix(col, base * 1.22, box(f, vec2(0.46, 0.84), vec2(0.54, 0.95), aa));
            col = mix(col, base * 1.15, box(f, vec2(-1.0, 0.0), vec2(2.0, 0.05), aa));
            shine = arch_;
          } else if (style > 7.5) {
            // 1930s: smooth render, wide three-pane windows, no shutters,
            // a thin band at each floor.
            if (gnd) {
              float shop = box(f, vec2(0.08, 0.06), vec2(0.92, 0.82), aa);
              col = mix(col, glass * 1.5 + vec3(0.04, 0.03, 0.02), shop);
              col = mix(col, base * 0.9, box(f, vec2(-1.0, 0.86), vec2(2.0, 1.0), aa));
              shine = shop;
            } else {
              vec2 lo = vec2(0.2, 0.22), hi = vec2(0.8, 0.8);
              float win = box(f, lo, hi, aa);
              col = mix(col, base * 0.93, box(f, vec2(-1.0, 0.9), vec2(2.0, 0.95), aa));
              col = mix(col, base * 1.1, box(f, lo - 0.025, hi + 0.025, aa));
              col = mix(col, glass, win);
              col = mix(col, vec3(0.92, 0.92, 0.88), win * (box(f, vec2(0.39, lo.y), vec2(0.405, hi.y), aa) + box(f, vec2(0.595, lo.y), vec2(0.61, hi.y), aa)));
              col *= 1.0 - 0.4 * win * box(f, vec2(lo.x, hi.y - 0.06), hi, aa);
              if (r < 0.3) col = mix(col, vec3(0.78, 0.66, 0.45) * mix(0.95, 0.9 + 0.1 * step(0.5, fract(f.y * 25.0)), fine), win * step(0.6, f.y));  // blind
              shine = win;
            }
          } else if (style > 5.5 && style < 6.5) {
            // Glass tower: bands of glass between slab edges.
            float slab = 1.0 - box(f, vec2(-1.0, 0.16), vec2(2.0, 0.92), aa);
            float mull = 1.0 - box(f, vec2(0.04, -1.0), vec2(0.96, 2.0), aa);
            vec3 g2 = mix(vec3(0.12, 0.17, 0.22), vec3(0.40, 0.52, 0.60), smoothstep(0.1, 1.0, f.y));
            g2 *= 0.85 + 0.3 * hash12(cell);
            col = mix(g2, base, clamp(slab + mull * 0.8, 0.0, 1.0));
            shine = 1.0 - clamp(slab + mull, 0.0, 1.0);
            farCol = mix(base, vec3(0.16, 0.22, 0.28), 0.45);
          } else if (style > 6.5) {
            // Contemporary (One Monte-Carlo, Mareterra): floor-to-ceiling
            // glass, crisp white slab edges, glass balustrades, thin fins.
            vec3 g2 = mix(vec3(0.10, 0.14, 0.18), vec3(0.42, 0.52, 0.58), smoothstep(0.1, 1.0, f.y));
            g2 *= 0.9 + 0.2 * hash12(cell);
            float slab = box(f, vec2(-1.0, 0.0), vec2(2.0, 0.08), aa);
            float balu = box(f, vec2(-1.0, 0.08), vec2(2.0, 0.34), aa) * step(0.5, lowF ? 0.0 : 1.0);
            float fin = 1.0 - box(f, vec2(0.035, -1.0), vec2(0.965, 2.0), aa);
            vec3 finC = vr < 0.6 ? vec3(0.9, 0.9, 0.88) : vec3(0.55, 0.42, 0.30);
            col = g2;
            col = mix(col, mix(col, vec3(0.75, 0.84, 0.86), 0.35), balu);
            col = mix(col, vec3(0.95), balu * box(f, vec2(-1.0, 0.32), vec2(2.0, 0.34), aa));
            col = mix(col, finC, fin);
            col = mix(col, base * 1.06, slab);
            shine = (1.0 - fin) * (1.0 - slab);
            farCol = mix(base, vec3(0.2, 0.26, 0.3), 0.4);
          } else if (style > 4.5) {
            // 1960s-80s towers: deep balconies running all round each
            // floor, solid or glass parapets, sliding windows behind and,
            // here and there, the striped awnings of the Riviera.
            float slab = box(f, vec2(-1.0, 0.0), vec2(2.0, 0.09), aa);
            float para = box(f, vec2(-1.0, 0.09), vec2(2.0, 0.38), aa);
            vec3 shade = base * 0.5;
            vec3 g2 = mix(vec3(0.08, 0.10, 0.12), vec3(0.28, 0.33, 0.38), smoothstep(0.4, 1.0, f.y));
            float win = box(f, vec2(0.05, 0.38), vec2(0.95, 0.94), aa);
            col = mix(shade, g2, win);
            col = mix(col, shade * 1.3, win * box(f, vec2(0.49, 0.38), vec2(0.51, 0.94), aa));
            col *= 1.0 - 0.3 * box(f, vec2(-1.0, 0.8), vec2(2.0, 1.0), aa);          // soffit shadow
            float awn = step(0.72, hash12(cell * 0.37 + vr * 9.0));
            vec3 awC = vr < 0.5 ? vec3(0.78, 0.48, 0.22) : vec3(0.32, 0.45, 0.42);
            float stripes = mix(0.92, 0.84 + 0.16 * step(0.5, fract(f.x * 4.0)), fine);
            col = mix(col, awC * stripes, awn * box(f, vec2(0.0, 0.62), vec2(1.0, 0.98), aa));
            vec3 paraC = vr < 0.55 ? base * 1.04 : mix(vec3(0.42, 0.50, 0.54), base, 0.25);
            col = mix(col, paraC, para);
            col = mix(col, base * 0.8, para * box(f, vec2(-1.0, 0.36), vec2(2.0, 0.38), aa));
            col = mix(col, base * 1.1, slab);
            float pier = (1.0 - box(f, vec2(0.04, -1.0), vec2(0.96, 2.0), aa)) * step(mod(cell.x, 4.0), 0.5);
            col = mix(col, base, pier);
            if (gnd) col = mix(base * 0.85, glass * 1.4, box(f, vec2(0.06, 0.06), vec2(0.94, 0.86), aa));
            shine = win * (1.0 - para) * (1.0 - awn);
            farCol = mix(base, base * 0.55, 0.35);
          } else {
            // Riviera residential (style 4): white slab edges, balcony
            // rails, deep windows behind, a solid pier every bay.
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
            if (gnd) col = mix(base * 0.8, glass * 1.3, box(f, vec2(0.08, 0.08), vec2(0.92, 0.85), aa));
            shine = win * (1.0 - rail);
            farCol = mix(base, base * 0.6, 0.35);
          }
          if (gnd && uv.y >= 0.0 && !(style > 1.5 && style < 3.5) && !(style > 5.5 && style < 6.5)) {   // not the Casino's arcades, villas or glass towers
            // Street level: every ground floor is a row of shops, as along
            // the circuit. Stone plinth, display windows with a lit
            // interior, a door every few bays, a painted sign band per
            // shop and striped awnings.
            float shopId = floor(cell.x / 2.0);
            float hs = hash12(vec2(shopId, vr * 53.0));
            vec3 signC = hs < 0.2 ? vec3(0.06, 0.20, 0.14) : hs < 0.4 ? vec3(0.05, 0.08, 0.20)
                       : hs < 0.6 ? vec3(0.32, 0.05, 0.07) : hs < 0.8 ? vec3(0.04, 0.04, 0.04) : vec3(0.55, 0.42, 0.16);
            vec3 stoneC = mix(base, vec3(0.80, 0.76, 0.68), 0.6);
            bool door = hash12(cell * 1.9 + 4.2) < 0.28;
            float plinth = 1.0 - step(0.07, f.y);
            vec2 wlo = door ? vec2(0.32, 0.07) : vec2(0.08, 0.10), whi = door ? vec2(0.68, 0.72) : vec2(0.92, 0.70);
            float win = box(f, wlo, whi, aa);
            float frameM = box(f, wlo - 0.03, whi + 0.03, aa);
            // Inside: warm light, shelves, a darker back.
            float shelves = mix(0.9, 0.8 + 0.2 * step(0.5, fract((f.y - wlo.y) * 6.0)), fine);
            vec3 inside = mix(vec3(0.20, 0.17, 0.13), vec3(0.55, 0.46, 0.34), smoothstep(wlo.y, whi.y, f.y) * 0.6) * shelves;
            inside = mix(inside, vec3(0.30, 0.34, 0.38), 0.35);          // reflection of the street
            vec3 sc = stoneC;
            sc = mix(sc, stoneC * 0.62, plinth);
            sc = mix(sc, mix(signC, vec3(0.12), 0.2), frameM);
            sc = mix(sc, door ? vec3(0.10, 0.08, 0.07) : inside, win);
            if (!door) sc = mix(sc, signC * 0.6, win * box(f, vec2(0.495, wlo.y), vec2(0.505, whi.y), aa));   // mullion
            // Sign band with a row of gold letters.
            float band = box(f, vec2(0.0, 0.76), vec2(1.0, 0.90), aa);
            float letters = step(0.5, fract(f.x * 9.0 + 0.25)) * box(f, vec2(0.2, 0.795), vec2(0.8, 0.865), aa)
                          * step(0.35, hash12(floor(vec2(f.x * 9.0, shopId * 3.0 + vr * 9.0)))) * fine;
            sc = mix(sc, signC, band);
            sc = mix(sc, vec3(0.86, 0.74, 0.44), letters * 0.9);
            // Awnings over about half the shops, in one colour per shop.
            if (hs > 0.45) {
              vec3 awC = hs < 0.6 ? vec3(0.62, 0.12, 0.10) : hs < 0.75 ? vec3(0.12, 0.30, 0.22) : hs < 0.9 ? vec3(0.80, 0.74, 0.62) : vec3(0.10, 0.16, 0.32);
              float stripes = mix(0.94, 0.80 + 0.2 * step(0.5, fract(f.x * 5.0)), fine);
              float aw = box(f, vec2(0.02, 0.62), vec2(0.98, 0.76), aa);
              sc *= 1.0 - 0.35 * box(f, vec2(0.04, 0.48), vec2(0.96, 0.62), aa) * win;      // its shade on the glass
              sc = mix(sc, awC * stripes, aw);
              sc = mix(sc, awC * 0.6, box(f, vec2(0.02, 0.62), vec2(0.98, 0.64), aa));    // valance edge
            }
            col = sc;
            shine = win * (door ? 0.3 : 1.0);
            farCol = mix(stoneC, signC, 0.25);
          } else if (lowF && uv.y >= 0.0 && !(style > 0.5 && style < 3.5) && !(style > 5.5 && style < 6.5)) {
            // No shop: the ground floor of flats, a hotel or offices. A
            // rendered stone base with rustication, barred windows and,
            // every few bays, a glazed entrance hall under a canopy.
            vec3 stoneC = mix(base, vec3(0.80, 0.76, 0.68), 0.35);
            float groove = 1.0 - 0.12 * (1.0 - smoothstep(0.0, 0.06, abs(fract(f.y * 4.0) - 0.5) * 2.0 - 0.88)) * fine;
            vec3 sc = stoneC * groove;
            sc = mix(sc, stoneC * 0.66, 1.0 - step(0.07, f.y));                 // plinth
            bool door = hash12(cell * 1.9 + vr * 13.0 + 4.2) < 0.16;
            vec3 g3 = mix(vec3(0.07, 0.08, 0.09), vec3(0.24, 0.28, 0.32), smoothstep(0.2, 1.0, f.y));
            if (door) {
              vec2 dlo = vec2(0.26, 0.07), dhi = vec2(0.74, 0.76);
              float d_ = box(f, dlo, dhi, aa);
              sc = mix(sc, stoneC * 1.12, box(f, dlo - vec2(0.05, 0.0), dhi + vec2(0.05, 0.04), aa));   // surround
              vec3 hall = mix(vec3(0.32, 0.27, 0.20), vec3(0.62, 0.55, 0.44), smoothstep(dlo.y, dhi.y, f.y) * 0.5);
              sc = mix(sc, mix(hall, vec3(0.30, 0.34, 0.38), 0.4), d_);
              sc = mix(sc, vec3(0.16, 0.13, 0.10), d_ * (box(f, vec2(0.49, dlo.y), vec2(0.51, dhi.y), aa)
                       + box(f, dlo, vec2(dhi.x, dlo.y + 0.03), aa) + box(f, vec2(dlo.x, dhi.y - 0.03), dhi, aa)));
              float canopy = box(f, vec2(0.16, 0.82), vec2(0.84, 0.87), aa);
              sc *= 1.0 - 0.3 * box(f, vec2(0.18, 0.76), vec2(0.82, 0.82), aa);   // its shadow
              sc = mix(sc, base * 1.1, canopy);
              shine = d_;
            } else {
              vec2 wlo = vec2(0.32, 0.30), whi = vec2(0.68, 0.80);
              float w_ = box(f, wlo, whi, aa);
              sc = mix(sc, stoneC * 1.1, box(f, wlo - 0.035, whi + 0.035, aa));
              sc = mix(sc, stoneC * 1.18, box(f, vec2(wlo.x - 0.05, wlo.y - 0.06), vec2(whi.x + 0.05, wlo.y - 0.02), aa));  // sill
              sc = mix(sc, g3, w_);
              sc *= 1.0 - 0.35 * w_ * box(f, vec2(wlo.x, whi.y - 0.06), whi, aa);
              float bars = step(0.72, fract(f.x * 26.0)) + box(f, vec2(wlo.x, 0.54), vec2(whi.x, 0.565), aa);
              sc = mix(sc, vec3(0.08), w_ * clamp(bars, 0.0, 1.0) * fine * 0.8);    // iron grille
              shine = w_;
            }
            col = sc;
            farCol = mix(stoneC, g3, 0.12);
          }
          if (style < 5.5 || style > 6.5) {
            // Weathering, kept subtle: plaster that varies in patches and
            // floor by floor, and rain streaks under the sills.
            vec3 pm = fsInput.attributes.positionMC;
            float patchN = vnoise(pm.xz * 0.35 + pm.y * 0.21) * 0.6 + vnoise(pm.xy * 1.3 + pm.zy * 0.7) * 0.4;
            float wall = 1.0 - shine;
            float floorTint = 0.97 + 0.055 * hash12(vec2(floor(uv.y), vr * 31.0));
            float streak = box(f, vec2(0.40, 0.0), vec2(0.60, 0.15), aa) * (0.5 + 0.5 * hash12(cell * 3.3 + 0.7))
                           * smoothstep(0.0, 0.15, f.y) * fine;
            float w = mix(0.90, 1.05, patchN) * floorTint * (1.0 - 0.14 * streak);
            col *= mix(1.0, w, wall * mix(0.55, 1.0, detail));
          }
          col = mix(farCol, col, detail);
          material.roughness = mix(0.85, 0.22, shine * detail);
          material.specular = mix(vec3(0.03), vec3(0.07), shine * detail);
          // Soft darkening at the foot of walls (ambient occlusion).
          col *= mix(0.72, 1.0, smoothstep(0.0, 1.2, uv.y));
          material.diffuse = col;
          if (u_night > 0.0 && uv.y > 0.0) {
            // Night: about half the flats lit, warm; a few cool TV-blue
            // ones; every shop on the ground floor.
            float lit = gnd ? 1.0 : (lowF && (style < 0.5 || style > 3.5) && !(style > 5.5 && style < 6.5)) ? step(hash12(cell * 1.7 + 2.0), 0.35) : step(hash12(cell * 1.31 + vr * 7.7), style > 5.5 && style < 6.5 ? 0.3 : 0.48);
            vec3 wc = mix(vec3(1.0, 0.72, 0.4), vec3(0.62, 0.78, 1.0), step(0.88, hash12(cell + 3.1)));
            wc *= 0.7 + 0.5 * hash12(cell * 2.7 + 1.3);
            material.emissive = wc * u_night * mix(0.16, shine * lit * 1.1, detail);
          }
          if (shopGlow > 0.0) {
            // Boutique display lights, on by day too.
            vec3 sp = fsInput.attributes.positionMC * length(czm_model[0].xyz);
            float spot = 0.75 + 0.25 * vnoise(sp.xz * 1.3 + sp.y * 0.8);
            material.emissive += vec3(1.0, 0.78, 0.48) * shopGlow * spot * mix(0.42, 1.3, u_night) * detail;
            material.diffuse = mix(material.diffuse, vec3(0.04), shopGlow * 0.5);
          }
        } else if (kind < 3.5) {        // flat roofs
          if (a255 - kind * 16.0 < 1.5) {
            // A real roof: its 20 cm aerial photo, from the roof texture.
            // Up close, a fine grain of gravel and membrane over it, so the
            // photo reads as a surface rather than a blur.
            vec3 pr = fsInput.attributes.positionMC * length(czm_model[0].xyz);
            float pxr = length(fwidth(pr.xz));
            float near = 1.0 - smoothstep(0.05, 0.3, pxr);
            vec3 d = material.diffuse;
            float grain = vnoise(pr.xz * 5.0) * 0.55 + vnoise(pr.xz * 19.0) * 0.45;
            // Which way the face slopes (flat-shaded triangles).
            vec3 nM = normalize(cross(dFdx(pr), dFdy(pr)));
            nM *= sign(nM.y + 1e-4);
            float pitched = 1.0 - smoothstep(0.94, 0.985, nM.y);
            float lum = dot(d, vec3(0.3, 0.59, 0.11));
            float terra = smoothstep(0.03, 0.10, d.r - d.b) * smoothstep(1.03, 1.15, d.r / (d.g + 0.02));
            vec3 det = vec3(0.9 + 0.18 * grain);
            vec3 tileF = vec3(1.0);
            // Tiles read from further away than gravel does.
            float nearT = 1.0 - smoothstep(0.1, 0.55, pxr);
            if (pitched > 0.0) {
              // Tiles: courses down the slope, each with a shadowed lower
              // lip; on terracotta, round Roman tiles across it; on slate
              // or zinc, standing seams running down the slope.
              vec2 dn = normalize(nM.xz + 1e-5);
              float sDown = dot(pr.xz, dn) / max(nM.y, 0.5);
              float sAlong = dot(pr.xz, vec2(-dn.y, dn.x));
              float aaS = fwidth(sDown) * 1.5, aaA = fwidth(sAlong) * 1.5;
              vec3 roman = romanTiles(sAlong, sDown, aaA, aaS);
              float dm = (0.5 - abs(fract(sAlong / 0.55) - 0.5)) * 0.55;    // metres to the nearest seam
              float seam = mix(0.72, 1.0, smoothstep(0.0, 0.025 + aaA, dm));
              vec3 metal = vec3(seam * (0.95 + 0.08 * grain));
              tileF = mix(vec3(1.0), mix(metal, roman, terra), nearT);
            }
            if (pitched < 1.0) {
              // Flat roofs, lined up with the building (TEXCOORD_1, metres):
              // pale warm terraces get square stone pavers with grout
              // lines; the rest waterproof membrane in rolled strips.
              vec2 f2 = fsInput.attributes.texCoord_1;
              vec2 aa = fwidth(f2) * 1.5;
              vec2 g = abs(fract(f2 / 0.6) - 0.5) * 0.6;
              float grout = (1.0 - smoothstep(0.012, 0.012 + aa.x, g.x)) + (1.0 - smoothstep(0.012, 0.012 + aa.y, g.y));
              float slab = 0.95 + 0.08 * hash12(floor(f2 / 0.6) + 7.0);
              vec3 paver = vec3(slab * (1.0 - 0.22 * clamp(grout, 0.0, 1.0)) * (0.96 + 0.06 * grain));
              float sx = abs(fract(f2.x / 1.0) - 0.5);
              float strip = 0.95 + 0.07 * hash12(vec2(floor(f2.x), 3.0));
              float seamM = mix(0.86, 1.0, smoothstep(0.0, 0.02 + aa.x, 0.5 - sx));
              vec3 membrane = vec3((0.88 + 0.22 * grain) * strip * seamM);
              float pale = smoothstep(0.5, 0.62, lum) * smoothstep(0.0, 0.05, d.r - d.b);
              det = mix(vec3(1.0), mix(membrane, paver, pale), near);
              // Terracotta seen on a flat-modelled roof is a tiled roof the
              // set keeps flat: tile courses lined up with the building.
              det = mix(det, mix(vec3(1.0), romanTiles(f2.x, f2.y, aa.x, aa.y), nearT), terra);
            }
            material.diffuse = d * mix(det, tileF, pitched);
            material.roughness = 0.8;
            return;
          }
          material.diffuse = base * (0.95 + 0.08 * hash12(floor(fsInput.attributes.positionMC.xz * 0.5)));
        } else if (kind < 4.5) {        // tiled roofs: rows of tiles
          float rows = fract(fsInput.attributes.positionMC.y * 3.0);
          material.diffuse = base * mix(0.86, 1.04, smoothstep(0.0, 0.25, rows));
          material.roughness = 0.75;
        } else if (kind < 5.5) {        // leaves
          // Leafy texture: clusters of leaves with darker gaps between
          // them, finer leaves up close, a warmer tint where the sun
          // shines through the edge of the crown.
          vec3 pm = fsInput.attributes.positionMC * length(czm_model[0].xyz);
          float cl = vnoise(pm.xz * 1.6 + pm.y * 0.9) * 0.6 + vnoise(pm.xz * 4.3 - pm.y * 2.1) * 0.4;
          float px_ = length(fwidth(pm));
          float leaf = mix(0.5, hash12(floor(pm.xz * 9.0 + pm.y * 7.0)), 1.0 - smoothstep(0.03, 0.12, px_));
          vec3 d = base * mix(0.62, 1.18, cl) * mix(0.88, 1.08, leaf);
          float rim = pow(1.0 - abs(dot(normalize(fsInput.attributes.normalEC), normalize(-fsInput.attributes.positionEC))), 2.0);
          d = mix(d, d * vec3(1.12, 1.15, 0.8), rim * 0.5);
          material.diffuse = d;
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
          // The water is shaded here, not by the PBR model: with a sky
          // that bright, PBR's Fresnel turned the whole port pale grey at
          // the low angles the cars and the phone camera see it from.
          // Colours from the IGN aerial photo and street photos: Port
          // Hercule a dark green-grey teal, the open sea a deep navy (about
          // #10324a), with only a soft, darkened reflection of the sky.
          vec3 nEC = material.normalEC;
          vec3 vEC = normalize(-fsInput.attributes.positionEC);
          float cosv = clamp(dot(nEC, vEC), 0.0, 1.0);
          float fres = 0.02 + 0.98 * pow(1.0 - cosv, 5.0);
          // Emissive is linear (0.055 shows as about 32/255 with HDR on desktop,
          // 66/255 on phones, which render without HDR).
          vec3 water = mix(vec3(0.014, 0.055, 0.066), vec3(0.010, 0.045, 0.095), far);
          vec3 skyRef = vec3(0.050, 0.100, 0.140);    // the sky, dimmed: water is never a mirror here
          vec3 wc = mix(water, skyRef, fres * 0.6);
          // Along the shore: clearer, lighter water, a line of foam where
          // it meets the quays and rocks, and small waves lapping in.
          float foam = 0.0;
          vec2 suv = fsInput.attributes.texCoord_0;
          if (suv.x > 0.0 && suv.x < 1.0 && suv.y > 0.0 && suv.y < 1.0) {
            // Read the right way up: the shore distance was sampled mirrored
            // north-south, so the harbour read "at the shore" and turned to
            // pale foam all over.
            float d = texture(u_lights, vec2(suv.x, 1.0 - suv.y)).g * 20.0;   // metres from the shore
            if (d < 19.0) {
              float shallow = 1.0 - smoothstep(2.0, 16.0, d);
              wc = mix(wc, vec3(0.022, 0.080, 0.085), shallow * 0.5);
              float br = vnoise(q * 0.9 + vec2(t * 0.15, -t * 0.1));
              float line = (1.0 - smoothstep(0.4, 1.8 + 0.8 * br, d));
              float lap = smoothstep(0.75, 0.95, 0.5 + 0.5 * sin(d * 1.3 - t * 1.6 + br * 2.0))
                        * (1.0 - smoothstep(1.5, 7.0, d)) * smoothstep(0.35, 0.65, vnoise(q * 1.7 - t * 0.2));
              foam = clamp(line * (0.55 + 0.45 * vnoise(q * 3.0 + t * 0.4)) + lap * 0.55, 0.0, 1.0) * near;
              wc = mix(wc, vec3(0.55, 0.60, 0.62), foam * 0.85);
            }
          }
          // Sun glints on the ripples.
          vec3 rEC = reflect(-vEC, nEC);
          float glint = pow(max(dot(rEC, czm_lightDirectionEC), 0.0), 400.0) * (1.0 - foam);
          wc += vec3(1.0, 0.92, 0.78) * glint * 1.5;
          material.diffuse = vec3(0.0);
          material.specular = vec3(0.0);
          material.roughness = 1.0;
          // Cesium's lighting still adds a grey sheen with no diffuse or
          // specular colour: Fresnel on the sun, and the sky light. A normal
          // turned away from the sun zeroes the first, zero occlusion the
          // second, so the water shows only the colour worked out here.
          material.normalEC = -normalize(czm_lightDirectionEC);
          material.occlusion = 0.0;
          material.emissive = wc * mix(1.0, 0.3, u_night);
        } else {
          float st = a255 - kind * 16.0;
          if (st > 0.5 && st < 2.5) {
            // Inside the tunnel: lit by its own lamps, not the sun.
            material.diffuse = base * 0.08;
            material.emissive = st > 1.5 ? vec3(1.0, 0.92, 0.72) : base * 0.55;
            material.roughness = st > 1.5 ? 1.0 : 0.6;
          } else if (st > 2.5 && st < 3.5) {
            // Street lamp head: lights up at night.
            material.diffuse = base * mix(0.8, 0.2, u_night);
            material.emissive = vec3(1.0, 0.88, 0.62) * 1.6 * u_night;
          } else if (st > 3.5 && st < 4.5) {
            // Spectators: matte clothes, dimmer at night.
            material.diffuse = base * mix(1.0, 0.35, u_night);
            material.roughness = 0.9;
          } else if (st > 4.5 && st < 5.5) {
            // Polished steel (Sky Mirror, Chrome Ball): mirrors the sky.
            material.diffuse = base * 0.25;
            material.specular = vec3(0.85);
            material.roughness = 0.12;
          } else if (st > 5.5 && st < 6.5) {
            // Bronze statues: dark, with a warm sheen.
            material.diffuse = base;
            material.specular = vec3(0.32, 0.24, 0.14);
            material.roughness = 0.42;
          } else if (st > 8.5 && st < 10.5) {
            // Kerb stones (uv: metres along, 0 road edge .. 1 back of the
            // top): pale granite in 1 m stones; style 10 painted in red
            // and white blocks, as round Monaco's corners.
            vec2 aa = max(fwidth(uv) * 1.2, vec2(0.002));
            float stone = fract(uv.x / 1.0);
            float jt = 1.0 - smoothstep(0.0, aa.x + 0.004, min(stone, 1.0 - stone) - 0.004);
            float h = hash12(vec2(floor(uv.x), 3.0));
            vec3 col = base * mix(0.92, 1.05, h) * mix(0.94, 1.04, hash12(floor(uv * vec2(40.0, 8.0))));
            if (st > 9.5) {
              float red = step(0.5, fract(uv.x / 2.0));
              col = mix(vec3(0.86, 0.85, 0.82), vec3(0.62, 0.06, 0.06), red) * mix(0.93, 1.03, h);
              col *= mix(0.9, 1.0, vnoise(uv * vec2(3.0, 9.0)));      // worn paint
            }
            material.diffuse = col * (1.0 - 0.3 * jt);
            material.roughness = 0.75;
          } else if (st > 10.5 && st < 11.5) {
            // Rubble stone retaining wall (uv: metres along, metres up):
            // irregular coursed limestone with recessed mortar.
            vec2 q = uv / vec2(0.62, 0.36);
            float row = floor(q.y);
            q.x += hash12(vec2(row, 1.7)) * 0.9;
            vec2 cell = floor(q), f = fract(q);
            vec2 j = vec2(hash12(cell + 0.5), hash12(cell + 2.1));
            f = (f - 0.5) * (1.0 + 0.18 * (j - 0.5)) + 0.5;
            vec2 aa = max(fwidth(q) * 1.2, vec2(0.002));
            float face = box(f, vec2(0.06, 0.09), vec2(0.94, 0.91), aa);
            float near = 1.0 - smoothstep(0.08, 0.3, max(aa.x, aa.y));
            float h = hash12(cell);
            vec3 col = base * mix(0.8, 1.12, h) * mix(0.9, 1.06, vnoise(uv * 4.0));
            col = mix(col, col * vec3(1.05, 0.98, 0.88), step(0.7, hash12(cell + 9.0)) * 0.5);
            col = mix(base * 0.62, col, mix(1.0, face, near));
            // Damp and dirt towards the foot.
            col *= mix(0.82, 1.0, smoothstep(0.0, 1.4, uv.y));
            material.diffuse = mix(base * 0.95, col, max(near, 0.35));
            material.roughness = 0.95;
          } else if (st > 11.5 && st < 12.5) {
            // Railing (uv: metres along, 0 foot .. 1 top): top and bottom rails,
            // bars every 13 cm, a post every 2 m; the gaps are see-through.
            // Far away, where the bars shrink under a pixel, a dither keeps
            // about the right amount of metal.
            vec2 aa = max(fwidth(uv) * 1.2, vec2(0.0015));
            float bars = 1.0 - smoothstep(0.012, 0.012 + aa.x, abs(fract(uv.x / 0.13) - 0.5) * 0.13);
            float post = 1.0 - smoothstep(0.03, 0.03 + aa.x, abs(fract(uv.x / 2.0 + 0.5) - 0.5) * 2.0);
            float rails = (1.0 - smoothstep(0.035, 0.035 + aa.y, abs(uv.y - 0.955)))
                        + (1.0 - smoothstep(0.02, 0.02 + aa.y, abs(uv.y - 0.1)));
            float m = clamp(bars + post + rails, 0.0, 1.0);
            if (aa.x > 0.05) {
              float cov = 0.025 / 0.13 + 0.06;
              m = max(m * step(aa.x, 0.13), step(fract(dot(gl_FragCoord.xy, vec2(0.5, 0.25))), cov));
              m = max(m, rails);
            }
            if (m < 0.5) discard;
            material.diffuse = base;
            material.roughness = 0.5;
            material.specular = vec3(0.06);
          } else if (st > 12.5 && st < 13.5) {
            // Grimaldi Forum glass (uv: metres). The entrance hall is a
            // lattice of triangles in pale steel; the ribbon under the
            // cornice (texCoord_1.x = 1) has upright mullions only.
            bool ribbon = fsInput.attributes.texCoord_1.x > 0.5;
            vec2 aa = max(fwidth(uv) * 1.2, vec2(0.002));
            vec2 q = uv / (ribbon ? vec2(1.6, 99.0) : vec2(2.4, 2.4));
            vec2 qa = aa / (ribbon ? vec2(1.6, 99.0) : vec2(2.4, 2.4));
            float w = ribbon ? 0.035 : 0.025;
            float lx = 1.0 - smoothstep(w, w + qa.x, abs(fract(q.x + 0.5) - 0.5));
            float ly = ribbon ? 0.0 : 1.0 - smoothstep(w, w + qa.y, abs(fract(q.y + 0.5) - 0.5));
            float qd = q.x + q.y;
            float ld = ribbon ? 0.0 : 1.0 - smoothstep(w, w + qa.x + qa.y, abs(fract(qd + 0.5) - 0.5));
            float frame = clamp(lx + ly + ld, 0.0, 1.0);
            float far_ = smoothstep(0.08, 0.3, max(qa.x, qa.y));
            frame = mix(frame, 0.12, far_);
            float pane = hash12(floor(q * vec2(1.0, 1.0)) + step(0.5, fract(q.x)) * 7.0);
            vec3 g = base * mix(0.9, 1.08, pane);
            material.diffuse = mix(g, ribbon ? vec3(0.2, 0.3, 0.23) : vec3(0.84, 0.86, 0.86), frame);
            material.specular = vec3(mix(ribbon ? 0.28 : 0.5, 0.08, frame));
            material.roughness = mix(0.1, 0.5, frame);
            // Lit from inside at night.
            material.emissive = vec3(1.0, 0.86, 0.62) * (ribbon ? 0.45 : 0.3) * u_night * (1.0 - frame);
          } else if (st > 13.5 && st < 14.5) {
            // Grimaldi Forum cladding (uv: metres along, metres up): green
            // metal panels with a horizontal fin every 0.9 m that throws
            // a thin shadow, joints every 3 m.
            vec2 aa = max(fwidth(uv) * 1.2, vec2(0.002));
            float fy = fract(uv.y / 0.9);
            float fin = 1.0 - smoothstep(0.06, 0.06 + aa.y / 0.9, fy);
            float shade = smoothstep(0.06, 0.06 + aa.y / 0.9, fy) * (1.0 - smoothstep(0.1, 0.32, fy));
            float joint = 1.0 - smoothstep(0.012, 0.012 + aa.x / 3.0, abs(fract(uv.x / 3.0 + 0.5) - 0.5));
            float near = 1.0 - smoothstep(0.05, 0.25, aa.y);
            vec3 col = base * mix(0.96, 1.04, hash12(floor(uv / vec2(3.0, 0.9))));
            col = mix(col, base * 1.18, fin * near);
            col *= 1.0 - 0.28 * shade * near - 0.2 * joint * near;
            material.diffuse = col;
            material.specular = vec3(0.12);
            material.roughness = 0.45;
          } else if (st > 6.5 && st < 7.5) {
            // Lit shop glass (the Casino's doors, the boutiques of the Allée
            // François Blanc): dark reflective panes with the warm light of
            // the shop inside, glowing more at night.
            vec3 p = fsInput.attributes.positionMC * length(czm_model[0].xyz);
            float glow = 0.8 + 0.2 * vnoise(p.xz * 0.35);
            // Display lights: brighter spots on a dim interior.
            float spot = smoothstep(0.5, 1.0, vnoise(p.xz * 0.9 + p.y * 0.4)) * 0.5;
            material.diffuse = vec3(0.03, 0.035, 0.04);
            material.specular = vec3(0.4);
            material.roughness = 0.1;
            material.emissive = base * (0.25 * glow + 0.35 * spot) * mix(0.75, 2.0, u_night);
          } else if (st > 14.5) {
            // Glazed roofs (the Casino's halls, the marquise): panes between
            // thin iron glazing bars in world metres, the sky in them.
            vec3 p = fsInput.attributes.positionMC * length(czm_model[0].xyz);
            vec2 q = p.xz / vec2(1.2, 0.75);
            vec2 aa = max(fwidth(q) * 1.2, vec2(0.002));
            float bx = 1.0 - smoothstep(0.04, 0.04 + aa.x, abs(fract(q.x + 0.5) - 0.5));
            float by = 1.0 - smoothstep(0.03, 0.03 + aa.y, abs(fract(q.y + 0.5) - 0.5));
            float bar = clamp(bx + by, 0.0, 1.0) * (1.0 - smoothstep(0.1, 0.35, max(aa.x, aa.y)));
            float pane = hash12(floor(q));
            vec3 g = base * mix(0.82, 1.08, pane) * mix(0.9, 1.05, vnoise(p.xz * 0.15));
            material.diffuse = mix(g * 0.7, vec3(0.16, 0.19, 0.18), bar);
            material.specular = vec3(mix(0.45, 0.06, bar));
            material.roughness = mix(0.08, 0.6, bar);
            material.emissive = vec3(1.0, 0.85, 0.6) * 0.35 * u_night * (1.0 - bar);
          } else {
            material.diffuse = base;
          }
        }
      }
    `,
  });

  let model = null, tube = null, loading = null, active = false, meta = null;

  // Game grade, in one full-screen pass: richer colour, an S-curve for
  // punchy contrast, golden highlights over teal shadows, warm haze that
  // grows with distance (from the depth buffer), and a soft vignette. At
  // night the haze turns blue and the shadows cool.
  let night = 0;
  const grade = scene.postProcessStages.add(new Cesium.PostProcessStage({
    fragmentShader: `
      uniform sampler2D colorTexture;
      uniform sampler2D depthTexture;
      uniform float u_night;
      in vec2 v_textureCoordinates;
      void main() {
        vec4 c = texture(colorTexture, v_textureCoordinates);
        vec3 col = c.rgb;
        float depth = czm_readDepth(depthTexture, v_textureCoordinates);
        if (depth < 1.0) {
          vec4 eye = czm_windowToEyeCoordinates(gl_FragCoord.xy, depth);
          float dist = length(eye.xyz);
          float haze = 1.0 - exp(-max(dist - 120.0, 0.0) / 1800.0);
          vec3 hz = mix(vec3(0.96, 0.84, 0.66), vec3(0.07, 0.09, 0.16), u_night);
          col = mix(col, hz, haze * mix(0.42, 0.55, u_night));
        }
        float l = dot(col, vec3(0.299, 0.587, 0.114));
        col = mix(vec3(l), col, mix(1.24, 1.1, u_night));
        col = mix(col, col * col * (3.0 - 2.0 * col), 0.35);           // S-curve
        vec3 lo = mix(vec3(0.93, 1.0, 1.04), vec3(0.85, 0.93, 1.12), u_night);
        vec3 hi = mix(vec3(1.07, 1.0, 0.88), vec3(1.06, 0.98, 0.9), u_night);
        col *= mix(lo, hi, smoothstep(0.1, 0.75, l));
        col = col * 1.03 + 0.01;
        vec2 d = v_textureCoordinates - 0.5;
        col *= 1.0 - dot(d, d) * mix(0.5, 0.75, u_night);
        out_FragColor = vec4(clamp(col, 0.0, 1.0), c.a);
      }`,
    uniforms: { u_night: () => night },
  }));
  grade.enabled = false;
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
      // The Grand Prix tunnel's inside: lit by its own lamps, no shadows.
      if (meta.tunnel) {
        tube = await Cesium.Model.fromGltfAsync({
          url: `${URL_BASE}-tunnel.glb`, modelMatrix: frame(),
          upAxis: Cesium.Axis.Y, forwardAxis: Cesium.Axis.X,
          customShader: shader, shadows: Cesium.ShadowMode.DISABLED, backFaceCulling: true,
        });
        tube.show = active;
        scene.primitives.add(tube);
      }
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
    if (active) shader.setUniform("u_crowd", M.tours && M.tours.isActive("legends") ? 1 : 0);
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
    if (M.cleanTrack && M.cleanTrack.on) M.cleanTrack.show(true);
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
      grade.enabled = true;
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
      if (tube) tube.show = true;
      if (clip) clip.enabled = true;
      watchFps();
      if (night > 0.5) applyNight(); else setBloom();
    } else {
      if (model) model.show = false;
      if (tube) tube.show = false;
      if (clip) clip.enabled = false;
      bloom.enabled = false;
      if (nightSaved) {
        const layers = viewer.imageryLayers;
        nightSaved.bright.forEach((b, i) => { if (i < layers.length) layers.get(i).brightness = b; });
        nightSaved = null;
      }
      if (M.legends && M.legends.skyNight) M.legends.skyNight(0);
      if (saved.time) {
        viewer.clock.currentTime = saved.time;
        scene.light = saved.light;
        grade.enabled = false;
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

  // ---- Night ----------------------------------------------------------
  // Late evening in June: the sun is down, a cool moon lights the set,
  // windows, shops and street lamps glow, and bloom makes the lights bleed.
  const bloom = scene.postProcessStages.bloom;
  function moonDir() {             // towards the moon, world axes
    const az = Cesium.Math.toRadians(150), el = Cesium.Math.toRadians(24);
    const toMoon = new Cesium.Cartesian3(Math.cos(el) * Math.sin(az), Math.cos(el) * Math.cos(az), Math.sin(el));
    const enu = Cesium.Transforms.eastNorthUpToFixedFrame(Cesium.Cartesian3.fromDegrees(7.428, 43.7395));
    const d = Cesium.Matrix4.multiplyByPointAsVector(enu, toMoon, new Cesium.Cartesian3());
    return Cesium.Cartesian3.normalize(d, d);
  }
  function moonLight() {
    const dir = Cesium.Cartesian3.negate(moonDir(), new Cesium.Cartesian3());
    return new Cesium.DirectionalLight({ direction: dir, color: new Cesium.Color(0.6, 0.7, 1.0), intensity: 0.65 });
  }
  const dayLight = () => new Cesium.SunLight({ color: new Cesium.Color(1.0, 0.93, 0.82), intensity: 2.6 });
  let nightSaved = null;
  function setBloom() {
    bloom.enabled = active && (night > 0.5 || !MOBILE);
    const u = bloom.uniforms;
    u.contrast = night > 0.5 ? 160 : 128;
    u.brightness = night > 0.5 ? -0.42 : -0.3;
    u.glowOnly = false;
    u.delta = 1.0;
    u.sigma = night > 0.5 ? 3.2 : 2.2;
    u.stepSize = 1.0;
  }
  function setNight(on) {
    night = on ? 1 : 0;
    shader.setUniform("u_night", night);
    document.body.classList.toggle("night", on);
    nightBtn.classList.toggle("on", on);
    if (!active) return;
    applyNight();
  }
  function applyNight() {
    const on = night > 0.5;
    if (on) {
      if (!nightSaved) {
        const layers = viewer.imageryLayers;
        nightSaved = { bright: [] };
        for (let i = 0; i < layers.length; i++) { nightSaved.bright.push(layers.get(i).brightness); layers.get(i).brightness = 0.22; }
      }
      viewer.clock.currentTime = Cesium.JulianDate.fromIso8601("2026-06-21T21:40:00Z");
      scene.light = moonLight();
      viewer.shadows = false;
      for (const m of [model, tube]) if (m && m.imageBasedLighting) m.imageBasedLighting.imageBasedLightingFactor = new Cesium.Cartesian2(0.25, 0.25);
    } else {
      if (nightSaved) {
        const layers = viewer.imageryLayers;
        nightSaved.bright.forEach((b, i) => { if (i < layers.length) layers.get(i).brightness = b; });
        nightSaved = null;
      }
      viewer.clock.currentTime = Cesium.JulianDate.fromIso8601("2026-06-21T06:15:00Z");
      scene.light = dayLight();
      viewer.shadows = true;
      for (const m of [model, tube]) if (m && m.imageBasedLighting) m.imageBasedLighting.imageBasedLightingFactor = new Cesium.Cartesian2(1, 1);
    }
    if (M.legends && M.legends.skyNight) M.legends.skyNight(night, moonDir());
    setBloom();
  }
  // "Night" sits under the view buttons while the film set is on.
  const nightRow = document.createElement("div");
  nightRow.className = "row film-only";
  const nightBtn = document.createElement("button");
  nightBtn.textContent = "☾ Night";
  nightBtn.title = "Switch the film set between day and night";
  nightRow.appendChild(nightBtn);
  row.parentElement.insertBefore(nightRow, row.nextSibling);
  nightBtn.onclick = () => setNight(!(night > 0.5));

  M.filmset = { setFilm, isOn: () => active, getModel: () => model, setNight, isNight: () => night > 0.5 };

  if (new URLSearchParams(location.search).get("night") === "1") setNight(true);
  if (new URLSearchParams(location.search).get("mode") === "film") {
    setFilm(true).then(() => {
      if (!new URLSearchParams(location.search).get("place")) {
        M.view(M.PLACES.find((p) => p.name === "Monte-Carlo"), 0);
      }
    });
  }
})();
