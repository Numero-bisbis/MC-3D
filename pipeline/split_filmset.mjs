// Split the film set (one big glb) into square tiles, so the viewer can
// stream it nearest-first and the GPU skips the tiles that are off screen
// (in the main view and in every shadow pass).
//
//   node pipeline/split_filmset.mjs viewer/filmset/monte-carlo.glb OUT_DIR
//
// Writes OUT_DIR/tiles/*.glb, the two textures once as OUT_DIR/monte-carlo-tex*.jpg
// (every tile points at them, so the browser fetches and uploads each once),
// and adds a "tiles" list to OUT_DIR/monte-carlo.json. Vertex data is copied
// as it is (same quantisation, same node transforms), so the tiles look
// exactly like the single file. The Pages workflow runs this at deploy time,
// so the committed glb stays the one source of truth.
import fs from "node:fs";
import path from "node:path";
import { Document, NodeIO } from "@gltf-transform/core";
import { ALL_EXTENSIONS, EXTMeshoptCompression, KHRMeshQuantization } from "@gltf-transform/extensions";
import { MeshoptDecoder, MeshoptEncoder } from "meshoptimizer";

const CELL = 250;          // metres
const ONE_PX = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=", "base64");
const [src, outDir] = process.argv.slice(2);
if (!src || !outDir) {
  console.error("usage: node split_filmset.mjs IN.glb OUT_DIR");
  process.exit(1);
}
await MeshoptDecoder.ready;
await MeshoptEncoder.ready;
const io = new NodeIO().registerExtensions(ALL_EXTENSIONS)
  .registerDependencies({ "meshopt.decoder": MeshoptDecoder, "meshopt.encoder": MeshoptEncoder });
const doc = await io.read(src);
const root = doc.getRoot();

// Textures: written once next to the tiles.
const texFiles = new Map();
root.listTextures().forEach((t, i) => {
  const ext = t.getMimeType() === "image/png" ? "png" : "jpg";
  const name = `monte-carlo-tex${i}.${ext}`;
  fs.writeFileSync(path.join(outDir, name), t.getImage());
  texFiles.set(t, name);
});

// Every triangle goes to the cell under its centre. Huge triangles (the sea
// plane) go to an always-loaded "base" tile.
const cells = new Map();   // key -> [{node, prim, tris: []}]
const parts = [];
for (const node of root.listNodes()) {
  const mesh = node.getMesh();
  if (!mesh) continue;
  const t = node.getTranslation(), s = node.getScale();
  for (const prim of mesh.listPrimitives()) {
    const pos = prim.getAttribute("POSITION");
    const idx = prim.getIndices().getArray();
    const n = pos.getCount();
    const P = new Float64Array(n * 3);
    const el = [];
    for (let i = 0; i < n; i++) {
      pos.getElement(i, el);
      for (let k = 0; k < 3; k++) P[i * 3 + k] = el[k] * s[k] + t[k];
    }
    const part = { node, prim, byCell: new Map() };
    parts.push(part);
    for (let f = 0; f < idx.length; f += 3) {
      const a = idx[f] * 3, b = idx[f + 1] * 3, c = idx[f + 2] * 3;
      const ext = Math.max(Math.abs(P[a] - P[b]), Math.abs(P[a] - P[c]), Math.abs(P[a + 2] - P[b + 2]), Math.abs(P[a + 2] - P[c + 2]));
      const cx = (P[a] + P[b] + P[c]) / 3, cz = (P[a + 2] + P[b + 2] + P[c + 2]) / 3;
      const key = ext > CELL * 2 ? "base" : `${Math.floor(cx / CELL)}_${Math.floor(cz / CELL)}`;
      let list = part.byCell.get(key);
      if (!list) { list = []; part.byCell.set(key, list); }
      list.push(idx[f], idx[f + 1], idx[f + 2]);
      if (!cells.has(key)) cells.set(key, { tris: 0, min: [Infinity, Infinity, Infinity], max: [-Infinity, -Infinity, -Infinity] });
      const cell = cells.get(key);
      cell.tris++;
      for (const v of [a, b, c]) for (let k = 0; k < 3; k++) {
        cell.min[k] = Math.min(cell.min[k], P[v + k]);
        cell.max[k] = Math.max(cell.max[k], P[v + k]);
      }
    }
  }
}

function copyAccessor(d, buf, a, remap, count) {
  const ElT = a.getArray().constructor;
  const size = a.getElementSize();
  const srcArr = a.getArray();
  const arr = new ElT(count * size);
  for (let i = 0; i < remap.length; i++) {
    const o = remap[i];
    if (o < 0) continue;
    for (let k = 0; k < size; k++) arr[o * size + k] = srcArr[i * size + k];
  }
  return d.createAccessor().setType(a.getType()).setArray(arr).setNormalized(a.getNormalized()).setBuffer(buf);
}

fs.mkdirSync(path.join(outDir, "tiles"), { recursive: true });
const tiles = [];
const imagePatch = [];
for (const [key, cell] of cells) {
  const d = new Document();
  d.createExtension(KHRMeshQuantization).setRequired(true);
  d.createExtension(EXTMeshoptCompression).setRequired(true)
    .setEncoderOptions({ method: EXTMeshoptCompression.EncoderMethod.FILTER });
  const buf = d.createBuffer();
  const scene = d.createScene();
  const mats = new Map();
  const texUsed = [];
  for (const part of parts) {
    const list = part.byCell.get(key);
    if (!list) continue;
    const { node, prim } = part;
    const n = prim.getAttribute("POSITION").getCount();
    const remap = new Int32Array(n).fill(-1);
    let count = 0;
    const out = new Uint32Array(list.length);
    for (let i = 0; i < list.length; i++) {
      const v = list[i];
      if (remap[v] < 0) remap[v] = count++;
      out[i] = remap[v];
    }
    const np = d.createPrimitive().setMode(prim.getMode());
    np.setIndices(d.createAccessor().setType("SCALAR")
      .setArray(count < 65536 ? Uint16Array.from(out) : out).setBuffer(buf));
    for (const sem of prim.listSemantics()) np.setAttribute(sem, copyAccessor(d, buf, prim.getAttribute(sem), remap, count));
    const m = prim.getMaterial();
    if (m) {
      if (!mats.has(m)) {
        const nm = d.createMaterial(m.getName())
          .setBaseColorFactor(m.getBaseColorFactor())
          .setRoughnessFactor(m.getRoughnessFactor())
          .setMetallicFactor(m.getMetallicFactor())
          .setDoubleSided(m.getDoubleSided());
        const tex = m.getBaseColorTexture();
        if (tex) {
          // A 1-pixel stand-in, swapped for a link to the shared file below.
          const nt = d.createTexture().setMimeType("image/png").setImage(ONE_PX);
          nm.setBaseColorTexture(nt);
          texUsed.push(texFiles.get(tex));
          const info = m.getBaseColorTextureInfo(), ninfo = nm.getBaseColorTextureInfo();
          ninfo.setTexCoord(info.getTexCoord()).setMagFilter(info.getMagFilter()).setMinFilter(info.getMinFilter())
            .setWrapS(info.getWrapS()).setWrapT(info.getWrapT());
        }
        mats.set(m, nm);
      }
      np.setMaterial(mats.get(m));
    }
    const nn = d.createNode().setTranslation(node.getTranslation()).setScale(node.getScale())
      .setRotation(node.getRotation()).setMesh(d.createMesh().addPrimitive(np));
    scene.addChild(nn);
  }
  const file = `tiles/${key}.glb`;
  await io.write(path.join(outDir, file), d);
  imagePatch.push([path.join(outDir, file), texUsed]);
  const c = cell.min.map((v, k) => (v + cell.max[k]) / 2);
  const r = Math.hypot(...cell.max.map((v, k) => v - cell.min[k])) / 2;
  tiles.push({ file, c: c.map((v) => Math.round(v)), r: Math.round(r), tris: cell.tris, bytes: 0 });
}

// Point each tile's images at the shared texture files (relative to the tile).
for (const [file, texUsed] of imagePatch) {
  const glb = fs.readFileSync(file);
  const jsonLen = glb.readUInt32LE(12);
  const json = JSON.parse(glb.subarray(20, 20 + jsonLen).toString("utf8"));
  (json.images || []).forEach((img, i) => {
    delete img.bufferView;
    delete img.mimeType;
    img.uri = `../${texUsed[i]}`;
  });
  let js = Buffer.from(JSON.stringify(json), "utf8");
  const pad = (4 - (js.length % 4)) % 4;
  js = Buffer.concat([js, Buffer.alloc(pad, 0x20)]);
  const rest = glb.subarray(20 + jsonLen);
  const header = Buffer.alloc(20);
  header.writeUInt32LE(0x46546c67, 0);
  header.writeUInt32LE(2, 4);
  header.writeUInt32LE(20 + js.length + rest.length, 8);
  header.writeUInt32LE(js.length, 12);
  header.writeUInt32LE(0x4e4f534a, 16);
  fs.writeFileSync(file, Buffer.concat([header, js, rest]));
}
for (const t of tiles) t.bytes = fs.statSync(path.join(outDir, t.file)).size;

const metaPath = path.join(outDir, "monte-carlo.json");
const meta = JSON.parse(fs.readFileSync(metaPath, "utf8"));
meta.tiles = tiles;
meta.tileSize = CELL;
fs.writeFileSync(metaPath, JSON.stringify(meta, null, 1));
const total = tiles.reduce((a, t) => a + t.bytes, 0);
console.log(`${tiles.length} tiles, ${(total / 1e6).toFixed(1)} MB, largest ${Math.max(...tiles.map((t) => t.bytes / 1e6)).toFixed(1)} MB`);
