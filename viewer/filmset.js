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
  // 5 leaves, 6 wood, 7 boat, 8 sea, 9 plain, 10 glass wall. For walls the
  // style is the façade type (pipeline S_*): 0 Riviera shutters, 1 Belle
  // Époque, 2 arched (Casino), 3 painted villa, 4 1960s-80s loggias,
  // 5 1970s balcony bands, 6 glass, 7 contemporary, 8 1930s.
  const shader = new Cesium.CustomShader({
    lightingModel: Cesium.LightingModel.PBR,
    uniforms: {
      u_time: { type: Cesium.UniformType.FLOAT, value: 0 },
      // 0 day, 1 night: lit windows, street lamps and their pools of light.
      u_night: { type: Cesium.UniformType.FLOAT, value: 0 },
      u_lights: { type: Cesium.UniformType.SAMPLER_2D, value: new Cesium.TextureUniform({ url: `${URL_BASE}-night.jpg` }) },
    },
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
      void fragmentMain(FragmentInput fsInput, inout czm_modelMaterial material) {
        vec4 c = fsInput.attributes.color_0;
        float a255 = floor(c.a * 255.0 + 0.5);
        float kind = floor(a255 / 16.0);
        if (kind < 0.5) {               // ground: the painted map
          material.roughness = 0.95;
          if (u_night > 0.0) {
            float L = texture(u_lights, fsInput.attributes.texCoord_0).r;
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
          bool gnd = uv.y < 1.0;
          float shine = 0.0;               // how much of this pixel is glass
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
            if (gnd) {
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
          } else if (style < 2.5) {
            // Casino / Café de Paris: round-arched windows between pilasters.
            float pil = 1.0 - box(f, vec2(0.08, -1.0), vec2(0.92, 2.0), aa);
            float archF = archMask(f, 0.2, 0.8, 0.08, 0.6, aa);
            float arch_ = archMask(f, 0.25, 0.75, 0.1, 0.6, aa);
            col = mix(col, base * 1.08, archF);
            col = mix(col, base * 1.12, pil);
            col = mix(col, base * 0.85, pil * box(f, vec2(0.08, -1.0), vec2(0.1, 2.0), aa));
            vec3 g = gnd ? glass * 1.5 + vec3(0.07, 0.05, 0.0) : glass;
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
            float balu = box(f, vec2(-1.0, 0.08), vec2(2.0, 0.34), aa) * step(0.5, gnd ? 0.0 : 1.0);
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
            float lit = gnd ? 1.0 : step(hash12(cell * 1.31 + vr * 7.7), style > 5.5 && style < 6.5 ? 0.3 : 0.48);
            vec3 wc = mix(vec3(1.0, 0.72, 0.4), vec3(0.62, 0.78, 1.0), step(0.88, hash12(cell + 3.1)));
            wc *= 0.7 + 0.5 * hash12(cell * 2.7 + 1.3);
            material.emissive = wc * u_night * mix(0.16, shine * lit * 1.1, detail);
          }
        } else if (kind < 3.5) {        // flat roofs
          if (a255 - kind * 16.0 < 1.5) {
            // A real roof: its 20 cm aerial photo, kept from the ground map.
            material.roughness = 0.8;
            return;
          }
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
          material.diffuse = mix(base, base * vec3(0.8, 0.95, 1.05), far) * 0.55 * mix(1.0, 0.4, u_night);
          material.roughness = 0.08;
          material.specular = vec3(0.04);
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
